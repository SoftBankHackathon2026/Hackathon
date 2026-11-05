# 시연 공통 계약: 환경변수와 Shared DB

2026-10-02 공통 연결 계약. 2026-10-03 온프렘 요구사항 변경: AWS/GCP의 Shared 연결 범위는 유지하며, 온프렘은 [앱별 PostgreSQL 컨테이너 생성 계획](ONPREM-DATA-SERVICES.md)을 따릅니다. 현재 요청 모델은 여전히 `shared`만 허용하므로 온프렘 자동 생성은 아직 구현된 기능이 아닙니다. 아래 온프렘 Shared 설명은 기존 Secret 연결 경로에 한정합니다.

2026-10-03 갱신: AWS만 앱 등록에서 Dedicated(앱별 RDS)와 캐시(ElastiCache Serverless) 선택을 지원합니다. 기본값은 계속 Shared이며, GCP·온프렘은 capabilities에 따라 Dedicated를 접수 전에 거부합니다.

## Shared의 의미

각 Provider에 준비한 PostgreSQL 인스턴스를 여러 앱이 공유하되, **앱마다 DB와 접속 계정을 분리**합니다. 서로 다른 앱이 같은 테이블이나 관리자 계정을 공유하는 뜻이 아닙니다. AWS·GCP·온프렘이 하나의 물리 DB를 함께 쓰는 뜻도 아닙니다.

Dedicated는 앱별 독립 인스턴스를 제공하는 후속 기능입니다. 공유 방식에서 전용 방식으로의 변경에는 데이터 이전이 필요하므로 단순 옵션 변경으로 처리하지 않습니다.

## 플랫폼과 인프라의 책임

| 주체 | 시연 준비 |
|---|---|
| 인프라 | Shared 인스턴스, 네트워크 연결, 앱별 DB·제한된 계정, 해당 앱의 Secret, 런타임의 읽기 권한 |
| 플랫폼 | 공통 요청 검증, Provider 선택, Secret 참조 연결, 배포 이력과 단계 표시 |
| 앱 | 환경변수 읽기, 포트에서 수신, health 경로 제공, 실제 DB 읽기·쓰기 시연 |

DB·계정 자동 발급은 아직 공통 Worker에 구현되지 않았습니다. 이번 경로는 **사전 발급 후 연결**입니다. Secret 참조가 등록되어 있다는 사실만으로 DB 접속·격리가 검증된 것은 아닙니다.

## 이미지가 읽는 공통 환경변수

| 이름 | 의미 | 전달 방식 |
|---|---|---|
| `PORT` | 앱 수신 포트 | 플랫폼이 주입 |
| `DATABASE_URL` | 앱 전용 PostgreSQL 접속 문자열 | Secret 참조 |
| `REDIS_URL` | Redis 접속 문자열 | 지원하는 Provider의 Secret 참조 |
| `LAUNCHPAD_RELEASE_ID` | 이번 릴리즈 식별자 | 플랫폼이 주입, 검증 응답 헤더와 연계 |

같은 이미지의 앱이 위 이름을 읽도록 맞춥니다. 호스트·계정·비밀번호 값은 Provider마다 다르며 이미지에 넣지 않습니다. 공통 `env`에는 일반 설정만 허용하고 위 네 이름은 거부합니다. 현재 GCP 연동 API는 새 환경변수나 릴리즈 식별자 주입을 지원하지 않으므로 완전한 공통 계약 준수 상태가 아닙니다.

AWS·온프렘 요청 예시(이미지·앱·환경은 실제 등록값 필요):

```json
{
  "application": "demo",
  "environment_id": "onprem-demo",
  "source": {"kind": "image", "image": "registry.example.com/demo@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
  "port": 8080,
  "health_path": "/healthz",
  "resources": {"database": "postgresql", "database_mode": "shared", "redis": false},
  "secret_bindings": [{"name": "DATABASE_URL", "binding": "db"}],
  "env": {"APP_COLOR": "blue"}
}
```

`binding`은 등록된 논리 이름입니다. 비밀번호나 Secret 원문을 요청에 넣지 않습니다. 기존 온프렘 환경이 다른 binding 이름을 쓰면 그 등록값을 사용합니다. 마이그레이션은 온프렘에서 별도 Job으로 실행 가능하고, AWS·현재 GCP API는 미지원입니다.

## 현재 코드에서 실제로 되는 범위

| 항목 | AWS | GCP | 온프렘 |
|---|---|---|---|
| 공통 Provider Registry·릴리즈 조회 | 연결 코드 있음 | 연결 코드 있음 | 연결 코드 있음 |
| 앱 입력 | ECR digest | 공개 GitHub 주소, 기존 demo | Registry digest |
| Shared DB 연결 | 운영자 `secret_refs.db` → 앱 등록 → `DATABASE_URL` | 기존 Cloud Run 설정 유지, 새 연결 요청 미지원 | 등록된 Kubernetes Secret → `DATABASE_URL` |
| Shared 앱 DB·계정 자동 발급 | 미구현 | 미구현 | 미구현 |
| Dedicated 사용자 선택 | 앱 등록에서 선택 → Worker가 RDS 생성 → `DATABASE_URL` | 거부 | 거부 |
| Redis 자동 발급 | 앱 등록에서 선택 → Worker가 ElastiCache Serverless(Valkey) 생성 → `REDIS_URL` | 미지원 | 미구현, 기존 Secret 연결 가능 |
| 실환경 종단 검증 | 이번 변경에서 미실행 | 이번 변경에서 미실행 | 이번 변경에서 미실행 |

AWS 웹의 PostgreSQL 선택은 더 이상 RDS 생성 요청이 아닙니다. 앱 전용 DB를 준비하고 고정 버전 Secrets Manager ARN을 운영자 설정의 `secret_refs.db`에 넣어야 합니다. Shared 참조를 등록하는 트랜잭션과 앱 등록을 함께 저장합니다. 누락되면 거부하며 Dedicated 생성으로 대체하지 않습니다. `db_state=ready`는 운영자가 준비한 연결 참조 상태이며 실접속 확인 결과가 아닙니다.

AWS Dedicated·캐시는 앱 등록(웹 `database_mode=dedicated`, `redis=true` 또는 CLI `register --database-mode dedicated --redis`)에서 선택합니다. HTTP는 선택만 기록하고 AWS를 호출하지 않으며, AWS Worker가 배포와 별개로 한 단계씩 생성합니다(RDS 약 7~10분, 캐시 약 2분). 준비 전 배포는 거절되고, 준비되면 고정 버전 Secret 참조로 `DATABASE_URL`·`REDIS_URL`을 주입합니다. 앱 삭제는 DB·캐시를 지우지 않으며 `db-delete`·`cache-delete`로 따로 삭제합니다. `legacy_dedicated_provisioning`은 기존 설정 파일 호환용으로만 남아 있고 더 이상 동작을 바꾸지 않습니다.

## 세 Provider를 완전히 맞추기 위해 남은 일

1. GCP 담당 API에 공통 배포 입력과 `DATABASE_URL` Secret 참조 전달을 추가합니다. 현재는 지원하지 않는 필드를 거부합니다.
2. 각 환경에 Shared 인스턴스와 앱별 DB·계정을 준비하고 런타임에서 실제 읽기·쓰기를 확인합니다.
3. 동일 DB 안에 다른 앱 계정이 접속·조회하지 못하는지 확인합니다. 앱용 계정에 관리자 권한을 주지 않습니다.
4. 배포·롤백 시 기존 DB를 재사용하며 앱 삭제로 Shared 인스턴스를 삭제하지 않는지 확인합니다.
5. 필요하면 후속 공통 Resource Provider에 앱별 할당·상태 조회·재시도·Secret 저장을 구현합니다. HTTP 접수 처리에서 DB를 동기 생성하지 않습니다.

## 팀에 전달할 문장

> 시연은 각 환경의 Shared PostgreSQL 인스턴스를 사용하고, 앱마다 DB와 접속 계정을 분리해서 준비해주세요. 앱은 공통으로 DATABASE_URL을 읽습니다. 플랫폼에는 비밀번호가 아닌 앱 전용 Secret 참조를 등록하고 배포 시 연결합니다. Dedicated와 앱별 DB 자동 발급은 이번 공통 구현 범위에서 제외하며, GCP는 새 Secret 연결을 받을 수 있도록 API 보완이 필요합니다.
