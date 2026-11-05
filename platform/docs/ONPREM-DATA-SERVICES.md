# 온프렘 앱 데이터 서비스 — 전달된 요구사항과 구현 계획

새 온프렘 리소스 정의와 실행 규격은 [Helm 인수인계](ONPREM-HELM-HANDOFF.md)로 구체화했습니다.
아래의 직접 SDK 템플릿 제안 대신 `oneport-app`·`oneport-data` 차트를 사용합니다.
앱별 데이터 소유권·보존·삭제 원칙은 유지하며, 플랫폼 API/배포 실행부의 Helm 연결은 별도 구현 항목입니다.

2026-10-03 인프라 담당자 안내를 사용자가 온프렘 구현 기준으로 확정했다. 이 문서는 구현 완료 기록이 아니다. 현재 플랫폼은 기존 immutable Secret 참조를 주입하고 Argo Rollout으로 웹 앱을 배포한다. 앱별 PostgreSQL·Redis 자동 생성과 완전 삭제는 아직 연결되지 않았다.

## 이번 안내로 확정된 범위

| 책임 | 범위 |
|---|---|
| 인프라 | K3s·스토리지·네트워크·공개 HTTPS·최소 RBAC |
| 플랫폼 | 웹 앱·앱용 PostgreSQL/Redis·Secret·상태 확인·완전 삭제 |
| 앱 | 포트·health 경로, `DATABASE_URL`·`REDIS_URL` 사용, 릴리즈 응답 헤더 |

확정된 온프렘 계약은 **앱별 전용 컨테이너**를 만드는 방식이다. 하나의 DB 인스턴스를 여러 앱이 공유하는 기존 Shared 데모 계약과 같지 않다. AWS/GCP의 공통 Shared 정책을 자동으로 Dedicated로 바꾸지 않는다. 구현 시 공통 요청에 온프렘 전용 컨테이너 방식의 지원 여부를 명시하고, 미지원 Provider에는 해당 요청을 거부해야 한다. 지금의 `database_mode=shared`에 전용 DB 생성 의미를 숨겨 넣지 않는다.

## 배포 단위와 초기 자원

### DB 종류와 할당 방식 구분

이번 구현 범위는 PostgreSQL이며 Redis는 선택적인 캐시 서비스다. MySQL·MongoDB 등은 아직 지원하지 않는다. 현재 공통 요청의 `database`는 `none` 또는 `postgresql`만 허용한다. PostgreSQL용 앱에 MySQL 접속 주소를 전달하는 방식으로 DB 종류를 바꿀 수는 없다. 드라이버·쿼리·마이그레이션 호환성을 앱에서 갖춰야 한다.

| 구분 | 의미 | 이번 범위 |
|---|---|---|
| 엔진 | PostgreSQL, MySQL 등 DB 종류 | PostgreSQL |
| 할당 방식 | 공유 인스턴스의 앱별 DB 또는 앱별 독립 실행 인스턴스 | AWS/GCP는 기존 Shared 연결, 온프렘은 앱별 컨테이너 생성 구현 예정 |
| 실행 위치 | DB를 어떤 환경에서 실행하는지 | 각 Provider 내부 |
| Secret | DB 접속 인증정보를 어디에 보관하고 어떻게 주입하는지 | 환경별 Secret 저장소의 참조 |

확장 시 공통 데이터 서비스 계층은 `준비/기존 자원 재사용 → 상태 확인 → 연결 Secret 참조 반환 → 소유한 자원 정리`를 담당한다. 엔진별 구현은 초기화·계정 발급·접속 문자열·인증 연결 검사를 담당하고, Provider별 구현은 실제 리소스와 Secret 저장소를 제어한다. 지원하지 않는 엔진·할당 방식의 조합은 리소스를 만들기 전에 거부한다. 이 구분은 구현 계획이며 공통 데이터 서비스 인터페이스가 완성됐다는 의미가 아니다.

온프렘 Secret에는 앱이 사용할 `DATABASE_URL` 또는 `REDIS_URL` 값을 저장하고 앱 Pod에 주입한다. 공개 앱 이미지에는 이 값을 넣지 않는다. 앱 데이터용 Secret과 레지스트리 pull 인증은 별개다. 공개 이미지 사용으로 pull 인증이 불필요해져도 DB Secret은 필요하다.

2026-10-03 기존 `oneport-platform.kubeconfig`의 `oneport-onprem` context로 `oneport-apps`의 StatefulSet create/patch, PVC create, Job delete가 허용됨을 `kubectl auth can-i`로 확인했다. 전체 동작 권한과 실제 생성은 구현 검증 시 추가 확인한다. 관리자 kubeconfig로 Worker 설정을 바꾸지 않았다. 권한이 생기는 것과 PostgreSQL 자동 생성 코드가 완성되는 것은 별개다.

| 대상 | 리소스 | CPU request / limit | 메모리 request / limit | 저장소 |
|---|---|---|---|---|
| 웹 앱 | Argo Rollout·active/preview Service·Ingress | 앱 요청 | 앱 요청 | 기본 없음 |
| PostgreSQL | 단일 StatefulSet·내부 Service·immutable Secret | 50m / 500m | 128Mi / 512Mi | local-path PVC 1Gi |
| Redis 캐시 | 단일 Deployment·내부 Service·immutable Secret | 10m / 200m | 32Mi / 128Mi | 없음 |

PostgreSQL: shared_buffers=32MB, max_connections=10, work_mem=1MB, maintenance_work_mem=16MB. Redis: maxmemory=16mb, allkeys-lru, AOF·snapshot 비활성화. 아직 이미지 초기화·실제 메모리·인증 연결을 검증한 값이 아니다. 이미지는 운영자 allowlist의 linux/amd64 digest로 고정해야 한다.

데이터 서비스에는 HTTP 200/릴리즈 헤더 규칙을 적용하지 않는다. PostgreSQL 인증 연결과 `SELECT 1`, Redis 인증 `PING`·쓰기·읽기를 별도 확인한다. Redis 캐시 데이터는 재시작 시 사라질 수 있다. 데이터 서비스에 외부 Ingress나 Tunnel을 생성하지 않는다.

## 코드 구성과 처리 순서

```text
공통 배포 요청
  → Provider 지원 범위·이미지·자원·권한 검사
  → 앱/환경별 데이터 서비스 작업 의도 저장
  → 기존 리소스 소유권·UID 확인 또는 생성
  → PostgreSQL/Redis 준비 상태 + 인증 연결 검사
  → immutable Secret name/key/UID를 런타임 binding에 저장
  → Migration Job
  → 기존 Argo Worker의 후보 검증·승격·공개 응답 확인
```

- `providers/onprem/data/`: 웹 앱 매니페스트와 분리한 데이터 서비스 템플릿·SDK 호출·관찰·소유권 검사.
- `execution/onprem/`: 데이터 준비와 앱 배포 단계를 저널에 기록하는 조정 로직. API는 의도만 접수하고 클러스터 작업을 기다리지 않는다.
- 플랫폼 DB에는 리소스 이름·UID·상태·Secret 참조만 저장한다. 비밀번호와 연결 URL 원문을 요청·이력·로그에 넣지 않는다.
- 리소스 이름은 앱+환경에서 안정적으로 파생한다. Secret이 이미 있으면 인증정보를 재사용한다. 생성 응답을 잃으면 같은 이름을 조회해 결과를 대조하고 비밀번호를 새로 설정하지 않는다.
- Secret만 사라지고 DB/PVC가 남은 경우 자동 재발급으로 복구하지 않는다. 데이터와 인증정보의 불일치로 표시하고 운영자 확인이 필요하다.
- 앱 재배포·블루그린·롤백 때 데이터 서비스와 PVC는 유지한다. 웹 앱의 두 버전이 같은 데이터 서비스를 사용한다.

## 완전 삭제

`삭제 요청 → 신규 배포 차단 → Worker와 앱 단위 잠금 → 앱 진입점/워크로드/Job 정리 → 데이터 서비스/Secret 정리 → PVC 정리 → 삭제 확인 → 완료`

삭제 대상은 저장된 앱·환경·리소스 UID로 확인한다. labels만 일치한다는 이유로 다른 리소스를 삭제하지 않는다. UID precondition으로 이름이 재사용된 리소스 삭제를 막는다. 404는 이미 삭제된 상태, 비동기 삭제 중이면 재조회, 결과 불명은 재대조한다. 데이터 서비스/PVC는 일반 배포 실패 정리 대상이 아니다.

플랫폼 자체 이력 DB와 공유 리소스는 삭제 대상에서 제외한다. PVC 삭제 전 사용자에게 데이터 영구 삭제를 명시하고 확인을 받아야 한다. 현재 플랫폼의 일반 배포 버튼에 삭제 동작을 연결하지 않는다.

## 구현·검증 순서

1. Worker 위치와 플랫폼 DB 접근 경로 확정. 이 Mac의 `127.0.0.1:55433`을 원격 Worker에 전달하지 않는다.
2. 데이터 서비스 권한은 2026-10-03 인프라 관리자가 기반 Role에 적용하고 실제 SSAR 및 기존 플랫폼 토큰의 server dry-run으로 확인했다. 새 클러스터에서는 [기반 설치](../../infra/onprem/README.md#기반-설치)로 RBAC와 Secret admission 정책을 함께 적용한 후 `infra/onprem/scripts/verify-foundation.sh`로 검사한다. [추가 RBAC](../examples/onprem/data-rbac-addon.yaml)는 이전 기반 Role의 호환용이다.
3. DB 없는 작은 웹 앱으로 플랫폼 API→이력 DB→Worker→Argo→preview→공개 HTTPS 흐름 검증.
4. 데이터 서비스 템플릿/저널/인증 검사를 구현하고, 동일 요청 재시도·응답 유실·리소스 교체·동시 요청을 테스트.
5. 앱 DB 연결, Migration, v2 데이터 유지, PostgreSQL 재시작 후 동일 PVC 데이터 유지 검증.
6. 삭제 전용 작업과 화면 확인을 구현하고 disposable 앱으로 완전 삭제·부분 실패 재시도 검증.

권한 확대만으로 데이터 서비스 기능을 준비 완료로 표시하지 않는다. Worker 경로와 실제 템플릿·연결·보존·삭제 동작을 별도로 검증한다. `local-path` 용량·복제·디스크 여유에 관한 수치는 전달된 인프라 보고이며, 플랫폼 계정으로 Node/PV를 조회해 재검증하지 않았다. 인프라 담당자가 앱 수용량을 확인한다.
