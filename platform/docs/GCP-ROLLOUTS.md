# GCP 배포 전략 · 설정과 실행

등록된 기존 Cloud Run 서비스에 새 이미지 리비전을 만들고, 플랫폼에 입력한 비율·시간·전환 방식으로 배포합니다. 카나리 진행 비율을 오류율로 자동 결정하지 않습니다. 인프라 Terraform이나 로드밸런서 규칙을 앱 배포마다 바꾸지 않습니다.

`cache_provisioning`이 등록된 환경은 플랫폼에서 Redis를 선택해 생성·연결할 수 있습니다.
기존 서비스의 캐시 Secret과 VPC 설정을 유지하며, 재배포는 같은 캐시를 재사용합니다.
리비전 실패로 캐시를 삭제하지 않으며, 삭제는 별도의 [캐시 SDK](../../infra/gcp/cache.md)를 사용합니다.

공개 Docker Hub digest도 `allowed_images`에 등록할 수 있습니다. [예제](../examples/gcp/request.json)는 DB 없는 공개 release-demo 이미지(8080, `/health`)를 사용하며, AWS·온프렘에도 같은 주소를 전달할 수 있습니다. Worker가 익명 접근·manifest/config digest·CPU 아키텍처를 검증한 뒤 후보를 생성합니다. Cloud Run 자체의 이미지 다운로드와 실제 응답까지 성공해야 배포 완료입니다. 서비스 UID·주소·DB·인증 설정은 실제 환경 값으로 바꿔야 합니다.

요청의 일반 `env`는 새 revision에 반영하고 기존 Secret·VPC·실행 계정은 보존합니다. 일반 env로 기존 Secret 참조를 덮어쓰는 요청은 거부합니다. `service_access: public`은 이미 공개된 서비스에서만 사용합니다. 이 설정은 IAM/ingress를 변경하지 않으며, UI와 HTTP probe가 로그인 없이 접근하도록 지정할 뿐입니다. 기본은 `authenticated`입니다.

## 지원 범위

| 설정 | 동작 |
| --- | --- |
| `strategy=canary` | 후보를 0%에서 검증 → 설정 비율 → 유지 시간 → 수동 또는 자동 100% 전환 |
| `strategy=blue_green` | 후보를 0%에서 검증 → 수동 또는 자동 100% 전환 |
| `rollout.canary_percent` | 새 버전 비중 1~99%, 기본 10% |
| `rollout.hold_seconds` | 실제 트래픽 분배 반영 후 유지하는 시간 0~3600초, 기본 300초. 카나리에만 적용 |
| `rollout.promotion` | `automatic`(기본): 카나리는 유지 시간 후, 블루그린은 후보 검증 후 100% 전환. `manual`: 전환 버튼 대기 |
| `rollout.bake_seconds` | 100% 전환 후 응답을 확인하는 시간 15~600초, 기본 60초 |
| `rollout.auto_rollback` | 기본 true. 배포 진행 중 새 버전 헬스체크가 연속 실패하면 이전 버전으로 복귀 |
| `rollout.failure_threshold` | 자동 복귀 연속 실패 횟수 2~10회, 기본 3회 |

`replicas`는 Cloud Run 리비전의 **최대 인스턴스 수**입니다. 고정 실행 수나 트래픽 비율이 아닙니다. 서비스 수준에서 별도로 설정한 확장 상한도 적용될 수 있습니다.

`rollout.promotion`을 생략하면 자동 전환합니다. 승인 후 전환하려면 `"promotion": "manual"`을 명시하거나 화면의 **배포 상세 설정**에서 수동 전환을 선택하세요. 요청 예제는 수동 전환을 명시한 예입니다.

자동 복귀를 꺼도 최초 후보 검증과 100% 전환 후 운영 응답 확인은 수행합니다. 실패하면 `awaiting_rollback`에서 멈추고 수동 복귀를 기다립니다. 카나리 유지 중에는 자동 복귀를 끈 경우 상태를 근거로 진행을 막지 않습니다. 성공한 배포를 계속 감시하는 서비스나 과거 릴리즈 선택 기능은 아닙니다.

## 운영자 환경 등록

1. [설정 예제](../examples/gcp/runtime.json)를 Git 밖에 복사해 실제 프로젝트·리전·앱·기존 서비스 UID·공개 HTTPS 주소·허용 이미지 digest·플랫폼 PostgreSQL DSN을 입력합니다. UID는 Cloud Run v2 `ServicesClient.get_service(...).uid`에서 읽습니다. 예제는 그대로 실행할 수 없습니다.
2. Cloud Run 서비스는 기존 버전 하나에 트래픽 100%인 안정 상태여야 합니다. `oneport-*` 이외의 트래픽 태그가 있으면 외부 관리와 충돌할 수 있어 접수 후 실행 검증에서 중단합니다. 처음 서비스를 생성하는 절차와 앱 계정 발급은 실행기 범위 밖입니다. 앱 전용 Redis는 아래 캐시 생성 설정이 필요합니다. 아래 DB 생성 설정을 등록하면 앱 전용 PostgreSQL을 준비할 수 있습니다. 그 외 기존 연결 설정은 유지됩니다.
3. Worker는 ADC로 인증합니다. `deployer_service_account`를 지정하면 해당 배포 계정을 impersonate합니다. 기존 배포 계정의 Cloud Run 관리·앱 계정 사용 권한을 사용하며 새 키는 필요하지 않습니다. IAM 인증이 켜진 서비스는 invoker 권한과 ID 토큰 발급 경로도 필요합니다.
4. Worker에서 후보 태그 `run.app` URL과 공개 HTTPS 주소에 모두 접근할 수 있어야 합니다. **현재 GCP 인프라의 `internal-and-cloud-load-balancing` ingress에서는 인터넷에 있는 로컬 Worker가 후보 URL을 바로 호출할 수 없습니다.** 같은 프로젝트 VPC 내부 경로 등을 준비합니다. Cloud Run에서 Worker를 실행하면 내부로 인정되는 VPC 경로를 통해 요청을 보내야 합니다. 후보 검증을 위해 앱 ingress를 외부 공개로 바꾸지 않습니다. [GCP 내부 접근 조건](https://docs.cloud.google.com/run/docs/securing/private-networking)
5. 앱의 `health_path`는 HTTP 200과 `X-Launchpad-Release` 응답 헤더를 반환해야 합니다. 헤더 값은 Worker가 넣는 `LAUNCHPAD_RELEASE_ID` 환경변수입니다. 준비 상태나 다른 버전의 응답만으로 새 버전 성공을 판단하지 않습니다. 후보 검증에 실패하면 새 버전에 사용자 트래픽을 보내지 않습니다.

API와 Worker에 같은 설정·DB를 사용합니다. 현재 설정 파일 하나는 앱/환경 하나를 등록합니다. 토큰과 DSN은 공유 문서에 넣지 않습니다.

```sh
export ONEPORT_RUNTIME_CONFIG=/safe/path/gcp-runtime.json
# ONEPORT_OPERATOR_TOKEN은 32자 이상의 운영자 토큰으로 별도 설정
uv run --project platform/backend uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
```

```sh
uv run --project platform/backend python -m controlplane.execution.gcp.operator worker \
  --config /safe/path/gcp-runtime.json
```

Worker 없이 API만 실행하면 접수한 배포는 진행되지 않습니다. 원격 Worker에서는 루프백 주소 대신 접근 가능한 플랫폼 DB를 사용합니다.

## 화면·API 사용

실제 배포 메뉴에서 등록된 GCP 환경을 고르고 카나리/블루그린, 비율, 유지 시간, 전환 방식, 자동 복귀를 지정합니다. 설정은 아래 요청 JSON에도 반영됩니다. [요청 예제](../examples/gcp/request.json)를 같은 형식으로 `POST /api/v1/releases`에 보낼 수도 있습니다. 요청마다 `idempotency_key`를 사용합니다.

- 수동 100% 전환: `POST /api/v1/releases/{id}/actions`에 `{"action":"promote"}`. 카나리는 유지 시간이 끝나고 `awaiting_promotion`에 도달해야 합니다.
- 수동 복귀: 같은 경로에 `{"action":"rollback"}`. 후보 검증·카나리 유지·수동 전환 대기·전환 후 확인·실패 대기 단계에서 가능합니다.
- 액션도 Bearer 운영자 토큰이 필요하며, 현재 환경 소유권·단계·설정 지문을 검사합니다. 응답의 `actions` 목록에 현재 가능한 버튼만 노출합니다.
- 실행 중 작업과 액션은 같은 DB 잠금으로 직렬화합니다. 일시적인 409는 상태를 갱신한 뒤 다시 시도합니다.

## 앱 전용 PostgreSQL 생성과 Notes 배포

GCP 환경에 `data_provisioning`이 등록되면 **배포 계획의 PostgreSQL 선택**과 **앱 → 배포하기 → 데이터베이스**가 활성화됩니다. 실제 배포에서 `PostgreSQL (Cloud SQL)`을 선택하면 먼저 DB를 만들고, 접속 정보를 연결한 후보를 검증합니다. 계획 확인만으로는 리소스를 만들지 않습니다.

운영자 설정은 `infra/gcp`의 출력값과 기존 앱 계정을 사용합니다. 현재 등록된 서비스 `oneport-demo`의 인프라 앱 ID는 `demo`입니다.

```json
"data_provisioning": {
  "app_id": "demo",
  "network": "projects/PROJECT_ID/global/networks/oneport",
  "subnetwork": "projects/PROJECT_ID/regions/REGION/subnetworks/oneport-cloud-run",
  "allocated_ip_range": "oneport-private-services"
}
```

실행 계정 `oneport-rt-<app_id>`와 앱별 Secret 읽기 권한, 배포 계정의 Cloud SQL·Secret 관리 권한, Direct VPC egress 서브넷과 Private Services Access는 인프라에서 먼저 준비해야 합니다. 앱 ID는 화면의 앱 이름과 구분합니다. 사용자 요청으로 계정·네트워크·DB 크기를 바꿀 수 없습니다.

요청의 `resources`는 `{"database":"postgresql","database_mode":"dedicated","redis":false}`입니다. Worker는 `oneport-db-<app_id>`에 PostgreSQL 17, `db-f1-micro`, SSD 10 GiB, ZONAL 구성을 만들고 자동 백업·저장 공간 자동 증가·삭제 보호를 켭니다. 공인 IP 없이 사설 IP와 암호화 연결을 사용합니다. 첫 생성에는 수 분이 걸리며 GCP 사용료가 발생합니다. HA 구성은 아닙니다.

`app` 데이터베이스와 앱 전용 SQL 사용자를 만든 뒤 `oneport_<app_id>_database` Secret의 **고정 버전**을 `DATABASE_URL`로 연결합니다. Secret 값은 플랫폼 DB·API·배포 이력에 저장하지 않습니다. Cloud Run 후보에는 같은 앱 실행 계정과 Direct VPC egress를 사용하며, 외부에서 지정한 DB 연결·다른 네트워크는 덮어쓰지 않습니다. SQL 사용자는 Cloud SQL 기본 인증의 `cloudsqlsuperuser` 역할을 사용하므로, 이 DB 인스턴스는 해당 앱만 사용해야 합니다. [Cloud SQL 사용자 권한](https://docs.cloud.google.com/sql/docs/postgres/users)

Notes FastAPI 이미지는 시작할 때 DB 대기와 Alembic 마이그레이션을 실행합니다. 이미지 주소는 빌드 결과의 digest를 그대로 사용하고, 상태 확인 주소는 **`/ready`**로 지정하세요. 이 경로가 DB 연결까지 확인합니다. `/health`는 프로세스 상태만 확인합니다. 이전 버전의 기준 확인·복구에는 이전 배포에 기록된 경로를 사용하므로, 기존 `/health` 앱에서 `/ready` 앱으로 바꿀 수 있습니다.

DB 준비 상태는 `data_preparing` 이벤트로 표시됩니다. 재배포·롤백에서는 같은 DB·사용자·Secret 버전을 재사용합니다. 실패한 앱 배포 때문에 DB를 삭제하거나 비밀번호를 변경하지 않습니다. 준비가 시작된 뒤에는 같은 DB 설정으로 재시도해야 하며, DB 삭제·설정 변경·암호 교체는 별도 운영 작업입니다. DB가 준비되었다는 사실과 후보 앱의 DB 연결 성공은 구분하며, 실제 성공 여부는 후보의 `/ready` 검사로 확인합니다.

Worker 재시작 시 Cloud SQL 작업 ID와 Secret 버전을 플랫폼 PostgreSQL의 `gcp_data`에서 복구합니다. SQL 사용자 생성 응답을 놓쳐도 기존 사용자를 초기화하지 않습니다. Secret 기록 후 사용자 생성 전에 중단되면 아직 앱에 전달되지 않은 계정 시도만 새로 만들며, 사용되지 않은 Secret 버전이 남을 수 있습니다. API 권한 오류·소유권 충돌은 기존 앱을 유지한 채 실패로 기록합니다. 외부 응답 본문과 비밀번호는 기록하지 않습니다.

## PostgreSQL migration

`data_provisioning`이 등록되고 전용 PostgreSQL을 선택한 환경에서 배포 전에 migration을 실행할
수 있습니다. 배포 화면에서 명령을 입력하거나 API의 `migration.argv`를 지정합니다. PR #77의
`GcpMigrationRunner`를 재사용해 후보 이미지의 Cloud Run Job으로 `DATABASE_URL`·사설 연결을
적용합니다. DB와 Redis 준비 후 migration이 성공해야 queued 상태로 돌아가 후보를 배포합니다.
실패·실행 불확실·기한 초과 시 후보 배포를 차단하고 기존 stable 트래픽을 유지합니다.
`backward_compatible=true`가 필수이며 DB downgrade·schema rollback은 구현하지 않습니다.

## 앱 전용 Redis 캐시

운영자 설정에 다음 항목을 등록하면 **배포하기 → 이 앱 전용 Redis 캐시 만들기**가 활성화됩니다.
DB 없이 Redis만 선택할 수 있으며, PostgreSQL을 함께 선택하면 두 연결을 같은 후보에 적용합니다.
`app_id`·네트워크·서브넷은 인프라 출력값을 사용하며, DB 설정이 있으면 그 값과 같아야 합니다.

```json
"cache_provisioning": {
  "app_id": "demo",
  "network": "projects/PROJECT_ID/global/networks/oneport",
  "subnetwork": "projects/PROJECT_ID/regions/REGION/subnetworks/oneport-cloud-run"
}
```

`infra/gcp`의 `oneport-cache-provisioning` Workflow와 실행 계정·Secret 권한을 먼저 적용합니다.
API와 Worker를 같은 버전·설정으로 갱신해야 하며, API 시작 시 `008_gcp_cache.sql`이 적용됩니다.
저장소의 배포 환경 설정에는 기존 `demo` 앱의 캐시 정책이 포함되어 있습니다.

Worker는 고정된 Workflow로 BASIC 1 GiB Memorystore를 만들고 완료까지 조회합니다.
Redis AUTH·TLS CA·사설 주소를 Secret Manager에 기록하고 **숫자 버전**을 Cloud Run의
`CACHE_CONFIG` 환경 변수에 연결합니다. `REDIS_URL`과 `/var/run/oneport/redis/ca.pem`의 TLS CA도 별도 Secret의 숫자 버전으로 자동 연결합니다. redis-py는 `redis.Redis.from_url(os.environ["REDIS_URL"])`로 사용할 수 있고 다른 클라이언트는 해당 CA 경로를 TLS 설정에 지정해야 합니다. 기존 JSON 연결 방식도 유지합니다.
일반 `REDIS_URL`과 형식이 다르며 [연결 예제](../../infra/gcp/integration/cache_client.py)를 참고합니다.
첫 생성에는 수 분이 걸립니다. 캐시는 앱이 유휴 상태여도 비용이 발생합니다.

Workflow 실행 이름·준비 단계·Secret 버전은 `gcp_cache`에 저장하고 비밀번호·CA 본문은 저장하지 않습니다.
재시작은 저장된 실행을 조회하며, 재배포는 같은 캐시와 Secret 버전을 사용합니다.
생성 POST의 응답을 잃으면 배포를 `uncertain`으로 기록하고 자동으로 재전송하지 않습니다.
429 응답이나 연결 수립 전 실패처럼 요청이 전달되지 않은 것이 확실하면 단계를 되돌리고 다음 시도에서 다시 제출합니다.
운영자는 해당 Workflow 실행 이력을 확인한 뒤, 확인된 실행 이름과 `creating` 단계를 저널에 복구해야 합니다.
Secret 버전 기록 응답을 잃은 경우에는 아직 앱에 연결하지 않은 버전만 다시 작성할 수 있습니다.

준비를 시작한 캐시의 선택 해제·크기 변경·자동 삭제는 지원하지 않습니다.
권한 오류, 외부 Secret 충돌, 네트워크 불일치 또는 Workflow 실패 시 후보 배포를 중단하고 기존 앱을 유지합니다.
캐시 준비 완료만으로 앱 연결 성공을 판단하지 않으므로, 앱의 준비 상태 검사에서도 필요한 연결을 확인해야 합니다.

## 실패와 재시작 처리

트래픽 변경 의도와 대기 종료 시각은 PostgreSQL에 기록합니다. Worker를 재시작해도 같은 후보 리비전을 재사용하고 남은 시간을 이어갑니다. Cloud Run API 응답을 놓친 경우 먼저 현재 설정을 조회해 반영 여부를 대조합니다.

서비스 UID, 후보 템플릿, 예상 트래픽이 달라지면 `uncertain`으로 멈추며 외부 변경을 덮어쓰지 않습니다. 앱/환경별 진행 중 배포가 있으면 새 요청을 막습니다. 이전 트래픽이 `LATEST`였어도 복귀 시에는 배포 전 리비전 이름으로 고정합니다.

자동 복귀는 **트래픽 복구**이며 DB·캐시 데이터나 스키마를 되돌리지 않습니다. 복귀 후 이전 버전의 운영 응답도 확인합니다. 기존 버전 응답까지 실패하면 복구 성공으로 기록하지 않습니다. 기본 전체 기한은 2시간이며 수동 대기도 포함됩니다. 자동 복귀가 켜진 안정된 대기 단계에서 기한이 지나면 복귀를 시도하고, 변경 결과를 확정할 수 없으면 `uncertain`으로 남깁니다.

## 검증과 후속 범위

테스트는 실제 PostgreSQL과 Google SDK protobuf 형식을 사용하는 Cloud Run 전송 대역을 사용합니다. 비율·시간·수동/자동 전환, 헬스체크 실패, 응답 유실 후 재시작, 외부 변경 충돌, 권한·환경 검증을 확인합니다. 실제 GCP에서 전체 경로를 실행한 결과와는 구분합니다.

실환경에서는 후보 URL의 내부 접근, 앱 응답 헤더, 커스텀 도메인의 트래픽 전환, 의도적 후보 실패 후 복귀를 검증해야 합니다. Cloud Monitoring의 5xx 비율·최소 요청 수·관측 지연을 활용하는 롤백, 리니어 배포, 신규 서비스 생성은 후속 범위입니다.

AWS ECS는 `deploymentConfiguration`의 전략·대기 시간·CloudWatch 알람을 서비스가 직접 처리합니다. 저장소의 [AWS 연동 안내](../../infra/aws/data-access.md)에 `BLUE_GREEN`/`CANARY`/`LINEAR`, `bakeTimeInMinutes`, `alarms.rollback=true`가 설명되어 있고 Terraform이 필요한 IAM 권한을 준비합니다. 공통 플랫폼의 AWS adapter/worker도 연결되어 있습니다. GCP 구현은 Cloud Run의 리비전 트래픽 API를 플랫폼 Worker가 순서대로 호출합니다. [Cloud Run 트래픽 제어](https://docs.cloud.google.com/run/docs/rollouts-rollbacks-traffic-migration)

## 공통 SDK 통합 시 선택

`engine=cloud_run` 설정은 이 문서의 native Google SDK 경로입니다. `ONEPORT_CONFIG`에서는 `runtime=gcp_cloud_run` 항목으로 등록합니다. GitHub URL을 받는 `gcp_bridge` 경로와 지원 계약이 달라 두 경로를 같은 플랫폼 설정에 함께 등록할 수 없습니다. bridge worker 명령은 `controlplane.execution.gcp_bridge.operator`입니다. 실제 배포 검증 기록의 Git 빌드/direct 성공을 이 native 카나리 경로의 실검증으로 간주하지 않습니다.

## 공개 health 경로

GCP native 요청의 기본값은 `/health`입니다. Cloud Run은 `/_ah/`로 시작하는 경로와 일부 `z`로 끝나는 경로를 예약합니다. 플랫폼은 공개 후보/서비스 URL을 검증하므로, `/_ah/*` 및 `z`로 끝나는 health 경로는 접수 전에 거부합니다. 앱이 제공하는 `/health` 또는 `/ready` 등으로 명시하세요. AWS·온프렘의 기본 경로는 변경하지 않습니다.

2026-10-03 실환경에서 동일 이미지·서비스의 `/`는 200, `/healthz`는 Google 404로 응답하고 `/health`는 앱의 릴리즈 헤더와 함께 200을 반환하는 것을 확인했습니다. Worker가 `baseline_unhealthy`로 중단한 뒤 경로를 수정해 검증을 이어갔습니다. 이 정책은 Google의 예약 경로 충돌 회피 권고를 적용한 것이며, `z`로 끝나는 모든 경로가 항상 차단된다는 뜻은 아닙니다. [공식 알려진 문제](https://docs.cloud.google.com/run/docs/known-issues#reserved_url_paths)

## Docker Hub 실환경 검증 — 2026-10-03

UI에서 접수한 `82bd6b1115f4bb47d99a0d39`는 native Worker에서 후보 URL 검증, 트래픽 100% 전환, bake 검증을 자동 완료했습니다. Cloud Run revision `oneport-registry-smoke-1003-op-82bd6b1115f4`에 요청한 Docker Hub digest가 실행됐고 공개 `/health` 200과 릴리즈 헤더가 일치했습니다. DB·Redis·카나리 검증은 포함하지 않았습니다. 검증용 서비스는 삭제했으며 팀의 기존 `demo`는 변경하지 않았습니다. [공통 검증 기록](NOTES-DEMO-DEPLOYMENT.md)
