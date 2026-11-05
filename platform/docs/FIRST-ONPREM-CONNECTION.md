# 클러스터 구성 다음: 플랫폼 첫 배포 연결

> 아래 진단 배경은 2026-10-02 당시 기록입니다. 현재 AWS/GCP 공통 통합 및 kubeconfig 접속은 완료되었습니다. 최신 현황은 [온프렘 접속 실측](ONPREM-CONNECTION-HANDOFF.md)과 [클라우드 통합](CLOUD-PROVIDER-INTEGRATION.md)을 우선 참고하세요.

2026-10-02 검토 기준: `main`의 플랫폼 PR #13(merge `556ac2d`)과 온프렘 인프라 PR #9(`31dfb76`, 검토 시 OPEN). 클러스터의 현재 상태를 직접 조회한 결과가 아니라 **저장소 코드와 인프라 전달 계약을 대조한 결과**입니다.

## 먼저 구분할 것

| 대상 | 지금 첫 앱 배포에 필요한가 | 현재 코드 |
|---|---|---|
| 플랫폼 PostgreSQL | 필요: 요청 큐·Release·이벤트·복구 체크포인트 저장 | `repositories/releases.py` 사용. 로컬 DB 도구 제공 |
| 사용자 앱 PostgreSQL | DB를 쓰는 앱만 필요 | 기존 immutable Secret으로 연결. DB 발급 없음 |
| 사용자 앱 Redis | Redis를 쓰는 앱만 필요 | 기존 Secret으로 연결. Redis 발급 없음 |
| Redis 작업 큐 | 필요 없음 | 현재 큐는 PostgreSQL 기반 |

첫 검증은 `resources.database=none`, `resources.redis=false`, `migration=null`인 앱으로 진행합니다. 플랫폼 DB까지 없애는 의미는 아닙니다.

## 코드에서 확인한 연결 장애물

1. **현재 API는 실제 환경 설정을 받지 않은 preview 상태.** `api.configured()`에 `ONEPORT_RUNTIME_CONFIG`를 제공해야 환경이 등록됩니다. 현재 파일 로딩은 앱/환경 하나를 등록하는 운영자용 방식이며, 앱·환경 생성 API/UI는 아직 없습니다.
2. **서비스 계정과 작업 권한.** `check_bindings()`는 DB 없는 앱도 `oneport-runtime`을 읽습니다. 현재 기반 Role은 이 조회와 Job 관리, StatefulSet·PVC 관리를 포함합니다. PR #9 당시 구성처럼 이전 Role을 사용하는 클러스터는 인프라 담당자가 기반 RBAC를 갱신해야 합니다. `examples/onprem/required-rbac-addon.yaml`은 이전 구성의 최소 호환 예제입니다.
3. **Worker의 실행 위치 미확정.** `ArgoRuntime.probe()`는 preview의 `*.oneport-apps.svc.cluster.local`을 직접 호출합니다. Tailscale로 API IP에 닿는 것만으로 DNS/Service 네트워크가 연결되지는 않습니다. 노드 호스트에서 실행하는 경우에도 DNS 설정은 별도 확인이 필요합니다.
4. **실제 이미지 부재.** 예제 JSON은 `registry.example.com`과 가짜 digest입니다. `examples/release-demo`를 linux/amd64로 빌드·push하고 실제 digest를 allowlist와 요청에 넣어야 합니다. 노드 pull 권한은 별도로 준비합니다.
5. **앱 응답 계약 차이.** 인프라 smoke는 http-echo/whoami로 경로를 검증합니다. 플랫폼은 `X-Launchpad-Release` 헤더까지 대조하므로 그 smoke 이미지를 그대로 넣으면 성공하지 않습니다. 제공한 release-demo는 이 계약을 구현합니다.
6. **Worker와 API가 같은 DB/환경을 사용해야 함.** API가 queue에 넣어도 Worker 프로세스가 없으면 실행되지 않습니다. Worker를 원격 VM/Pod로 옮길 경우 Mac의 `127.0.0.1:55433` DSN을 그대로 쓸 수 없습니다.

## 첫 연결 순서와 담당

| 순서 | 담당 | 작업 | 완료 증거 |
|---|---|---|---|
| 1 | 인프라 | 플랫폼 전용 kubeconfig·CA·API 주소·context·토큰 갱신 방식 전달 | 관리자 권한 없이 필요한 API 조회 가능 |
| 2 | 인프라 + 플랫폼 | runtime SA 조회 권한 보완, Worker 위치와 DB 접근 경로 확정 | 실제 Worker 위치에서 DB/클러스터 사설 서비스 접근 |
| 3 | 플랫폼 | DB 없는 demo 이미지 빌드·push, digest 등록 | 클러스터 노드 pull 가능 |
| 4 | 플랫폼 | app/env/hostname/자원/이미지 등록, API·Worker 실행 | 화면에 등록 환경 표시, 진단 통과 |
| 5 | 플랫폼 | 화면에서 v1 배포 | 이력 succeeded + 실제 HTTPS 응답/Release ID 일치 |
| 6 | 플랫폼 + 인프라 | v2 Blue/Green·불량 후보·Worker 재시작 검증 | 후보 공개 전 검증, 기존 버전 유지/복원, 중복 작업 없음 |
| 7 | 별도 데이터 계층 작업 | PG/Redis·Secret 준비 후 Migration 추가 검증 | DB 읽기/쓰기 및 실패 시 승격 중단 |

추천 실행 위치는 **Worker를 K3s 안에 두는 것**입니다. preview를 외부로 공개할 필요가 줄어듭니다. 다만 현재 저장소에는 플랫폼 backend/Worker의 컨테이너 배포 패키지와 플랫폼 DB의 클러스터 운영 구성이 없으므로, 이것도 플랫폼 측 구현 작업입니다. API와 Worker를 같은 사설 네트워크에 두고 UI는 로컬에서 API 터널로 연결하는 구성이 첫 연동에 적합합니다. kubeconfig를 명시적으로 마운트하는 현재 인증 방식의 배포·갱신 절차도 준비해야 합니다.

Mac에서 Worker를 실행하는 방식을 선택할 수도 있지만, 인프라가 Service CIDR 라우팅과 클러스터 DNS까지 제공하고 검증해야 합니다. 연결 편의를 위해 preview를 공개하거나 TLS 검증을 끄지는 않습니다.

근거: Kubernetes는 Service에 클러스터 DNS 이름을 부여하지만, 노드의 DNS 설정까지 기본으로 변경하지는 않습니다. [Service DNS](https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/), [DNS 진단](https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/).

## 추가한 진단 명령

**Worker를 실행할 컴퓨터/Pod에서**, 실제 runtime/요청 파일로 실행합니다. 별도 리소스 생성, 배포 요청, DB 마이그레이션은 하지 않습니다.

```sh
uv run --project platform/backend python -m controlplane.execution.onprem.doctor \
  --config /safe/path/runtime.json --request /safe/path/request.json
```

다음을 구분해 JSON으로 출력합니다.

- 등록 정책: 앱/환경/digest/자원/Secret UID.
- 플랫폼 PostgreSQL 연결과 이력 테이블 존재 여부. API를 먼저 시작하면 테이블을 준비합니다.
- kubeconfig·CA·API 접근과 필요한 RBAC/Secret 참조. 권한 거절이면 어떤 resource/verb인지 표시.
- `kubernetes.default.svc.<cluster-domain>` DNS와 TCP 연결.
- 할당 hostname의 DNS와 공개 443 TCP 연결.

`checks_passed=true`는 위 사전 검사만 통과했다는 뜻이며 **실제 앱 배포 성공이 아닙니다**. 기본 Kubernetes Service에 닿아도 앱 preview의 NetworkPolicy/HTTP 응답은 별도 검증해야 합니다. 노드 image pull, 남은 용량, Argo 동작, 공개 TLS/앱 응답, DB/Redis 사용은 첫 배포에서 확인합니다. 결과의 `application_verified`는 항상 false입니다. DSN·토큰·Secret 값과 원시 예외 메시지는 출력하지 않습니다.

## 플랫폼에 남은 개발

첫 배포를 위해서는 **Worker 배포 패키징·실환경 설정 연결·테스트 이미지 발행·통합 검증**을 먼저 처리합니다. 환경별 값은 운영자가 등록하는 방식으로 시작할 수 있습니다.

사용자가 앱을 자유롭게 추가하는 제품으로 만들려면 아래가 추가로 필요합니다.

- 앱/환경 등록 API·UI와 저장 모델, hostname 할당·UNIQUE·재시도 시 재사용.
- Worker 운영 상태 표시. 현재 화면의 등록 여부는 Worker 생존이나 클러스터 연결 성공을 뜻하지 않음.
- 과거 성공 Release 선택 → 새 복구 작업 생성하는 롤백 API/UI. 현재는 배포 실패 시 이전 성공 요청 복원만 있음.
- uncertain 작업 대조·취소/복구, 오래된 리소스 정리.
- PG/Redis 발급 기능을 포함한다면 데이터 계층의 저장소·용량·백업·사용자 격리·삭제 정책 합의 및 provisioning 구현. 현재 `secret_bindings`는 이미 있는 리소스 연결만 처리.
- 서비스 공개 전 사용자/팀 인가와 격리. 현재 로컬 운영자 토큰과 공유 앱 namespace는 운영 멀티테넌시가 아님.

## AWS/GCP 코드와 합칠 때의 확인 사항

AWS Terraform은 공용 ECS/ALB/권한을 만들고 앱별 ECS 서비스·target group·host rule·runtime IAM role·DB/Secret은 플랫폼 생성 범위로 남겨둡니다. AWS runtime adapter/worker는 현재 `platform/`에 없습니다.

GCP에는 `infra/gcp/integration/sdk.py`의 실제 SDK 검증 코드와 `provision_app.py`의 앱 계정 발급 호출 예제가 있습니다. 이 코드는 플랫폼 큐/Registry와 연결된 GCP Worker가 아닙니다. 검증 SDK는 8080 포트, `/health`, CPU 1, 메모리 512Mi, 최대 인스턴스 1 등 테스트 값을 사용합니다. 이를 그대로 공통 요청 구현으로 간주하면 안 됩니다. 운영 도메인용 ingress/auth와 사용자 자원 요청을 담당 adapter가 매핑해야 합니다.

공통 모델의 앱 이름 상한은 30자지만 GCP 계정 발급 계약은 19자이며, 온프렘 replicas는 고정 개수이고 Cloud Run 설정은 최소/최대 인스턴스입니다. AWS/GCP 담당자와 검증/화면 계약을 맞춰야 하며 단순 파일 복사만으로 통합되지 않습니다. 이번 연결 작업에서는 다른 담당자의 구현을 변경하지 않습니다.
