# GCP 실행 연계

React `:4320` → OnePort API `:8320` → PostgreSQL ReleaseStore → GCP worker
→ loopback HTTP GCP Provider `:8331` → Docker / Artifact Registry / Cloud Run SDK.
Provider는 `gcp-deployment-lab`의 `main`을 사용합니다.
이 문서의 기준 커밋은 `cfc9b6a133cc585308581c4fb3a18b2bf80fc5fb`입니다.
Repository 기반 실행은 `execution/gcp_bridge`와 `providers/gcp_bridge`에만 있습니다.
OnePort의 native `execution/gcp`와 `providers/gcp`는 image 기반 Cloud Run SDK,
configurable rollout, manual promote/rollback 계약을 그대로 유지합니다.
AWS·On-Prem과 공통 DeploymentProvider API를 사용하며 두 GCP 입력 방식을 섞지 않습니다.

## 실행

별도 터미널에서 GCP provider를 실행합니다. 설정과 service-account key는 Git 외부에 둡니다.
실제 키 내용과 경로는 OnePort 요청이나 프런트엔드에 전달하지 않습니다.

```powershell
cd <gcp-deployment-lab>\backend
$env:GCP_CONFIG_FILE="<외부 환경 설정 JSON>"
$env:GOOGLE_APPLICATION_CREDENTIALS="<저장소 외부 service-account key>"
python -m scripts.verify_gcp_adc  # 읽기 전용 SDK 연결 확인
python -m uvicorn app.main:app --host 127.0.0.1 --port 8331 --workers 1
```

OnePort 루트에서 로컬 DB를 준비합니다.

```powershell
python platform/scripts/local_database.py
```

API와 worker 터미널 각각에 같은 환경을 설정합니다. DB URL은 운영자가 주입하거나
로컬 `.data/database.json`에서 읽습니다. 토큰은 32자 이상이고 출력·커밋하지 않습니다.

```powershell
$env:ONEPORT_OPERATOR_TOKEN="<운영자가 발급한 32자 이상 플랫폼 토큰>"
$env:ONEPORT_CONFIG="<Git 외부 공통 platform config JSON>"
uv run --frozen --project platform/backend uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
# 별도 터미널, 동일 환경
uv run --frozen --project platform/backend python -m controlplane.execution.gcp_bridge.operator worker --config "<Git 외부 bridge runtime JSON>"
# 프런트엔드 별도 터미널
cd platform/frontend
npm ci
npm run dev
```

`platform/examples/gcp-bridge/runtime.json`을 Git 외부에 복사하고 실제 PostgreSQL DSN을
운영자가 주입합니다. API bundle은 동일 DSN의 `database_url`과
`providers: [{"runtime":"gcp_cloud_run","config_file":"<bridge runtime JSON>"}]`을 사용합니다.
AWS·On-Prem provider는 기존 bundle 항목으로 함께 등록합니다. 환경이 없으면 preview입니다.
Native GCP는 `engine="cloud_run"`인 별도 설정을 사용합니다. Bridge 설정에는 `engine`이 없습니다.
한 runtime에 상이한 capabilities를 동시에 등록하는 것은 현재 registry가 지원하지 않습니다.
`ONEPORT_GCP_PROVIDER_URL` 등 개인 POC의 자동 등록 환경변수는 실행 계약에서 제거했습니다.
등록 상태는 API/SDK 연결 성공을 뜻하지 않습니다. 실제 cloud 연결은 provider 검증 단계에서 확인합니다.

## 공통 API와 UI

UI의 실제 배포 메뉴에서 GCP 환경, 공개 GitHub 저장소, Blue/Green을 선택합니다.
앱은 demo 고정이며 root Dockerfile과 `/health`를 사용하는 검증된 provider 계약입니다.
자원 설정은 provider가 관리하므로 bridge 요청에 native rollout·자원 옵션을 넣지 않습니다.
이 bridge 경로의 DB/Redis/Secret/Migration은 미연결입니다. Native worker의 PostgreSQL·Redis·migration 연결은 [GCP 실행 안내](GCP-ROLLOUTS.md)를 참고하세요.

`POST /api/v1/releases`에 operator Bearer token과 다음 공통 계약을 전송합니다.

```json
{
  "idempotency_key": "unique-request-key-0001",
  "request": {
    "application": "demo",
    "environment_id": "gcp-demo",
    "source": {"kind": "repository", "repository": "https://github.com/owner/app.git"},
    "strategy": "blue_green"
  }
}
```

202 결과의 release `id`로 `GET /api/v1/releases/{id}`를 조회합니다.
브라우저는 기존 `/api` proxy만 사용하고 provider URL을 호출하지 않습니다.
RepositorySource는 기본 브랜치 HEAD를 사용합니다. 공통의 고정 commit GitSource와 다릅니다.
UI는 환경의 `input_kind`와 `request_template`, capabilities로 입력과 설정을 결정합니다.
On-Prem은 기존 image digest 계약을 그대로 검증합니다.

Worker는 최초 전송 의도부터 DB에 commit하고 POST를 최대 한 번 실행합니다.
`remote_id`가 저장되면 이후 tick과 worker 재시작은 GET만 수행합니다.
POST 응답 유실, 재시작 후 404, 알 수 없는 상태/관측 결과는 `uncertain`입니다.
GET의 일시적 장애는 기존 공통 reconciler의 제한된 재조회만 수행하며 POST는 재실행하지 않습니다.
자동 재접수하거나 새 Cloud Run revision을 생성하지 않습니다.

상태는 queued → cloning → building → pushing → deploying → verifying → promoting →
checking → succeeded입니다. 실패 복구는 checking → restoring → restore_checking → failed,
`rollback_status=SUCCESS`로 표시합니다. 복구된 실패 배포를 성공으로 바꾸지 않습니다.
진행 단계는 provider history에서 빠짐없이 가져오되 자체 메시지와 검증된 메타데이터만 저장합니다.
project, commit, image/digest, revision·이전/후보/운영 revision, health, rollback_status,
등록된 service URL만 structural allowlist로 노출합니다. Digest는 image URI에서 유도합니다.
raw error, credential, token, arbitrary log/history body는 저장/반환하지 않습니다. 성공 증거 URL/revision/digest/health가
불완전하면 succeeded 대신 uncertain입니다.

## 제한과 검증

2026-10-03 demo 공개 접속 검증: `invoker_iam_disabled=true`를 운영자가 서비스 수준에서
한 번 적용했습니다. 인증/비인증 `/`, `/health` 모두 200이며 production
`demo-cb-3e21125a` 100%, revision 9개를 유지합니다. 적용 방식과 권한·정책 조회 제한은
[GCP 인프라 운영 기록](../../infra/gcp/README.md#demo-공개-접속-운영-기록)에 있습니다.
배포 provider가 이 설정을 자동으로 변경하거나 deployer 권한을 확장하지 않습니다.

검증된 demo의 bridge runtime은 `service_access="public"`입니다. 예제에도 이 운영 상태를
기록했습니다. 다른 서비스는 확인한 접근 정책을 명시하며 기본값은 `authenticated`입니다.
이 필드는 UI 표시만 결정하며 Cloud Run 설정/IAM을 변경하지 않습니다.

OnePort release/checkpoint/events는 PostgreSQL에 영속화됩니다. GCP provider job store와
rollback checkpoint는 process-local이고 single worker입니다. provider 서버 재시작 복구는
지원하지 않습니다. `uncertain` release는 같은 환경의 새 배포를 차단하며 운영자의 실제
Cloud Run 대조가 필요합니다. 자동 timeout 재배포, job 복구, uncertain 해소 UI는 없습니다.
12분 OnePort deadline 이후에는 uncertain으로 남고 진행 중 cloud operation을 취소하지 않습니다.

```powershell
uv run --frozen --project platform/backend ruff check platform/backend
uv run --frozen --project platform/backend ruff format --check platform/backend
uv run --frozen --all-extras --project platform/backend pytest -q platform/backend/tests
uv build --project platform/backend
cd platform/frontend
npm run format:check
npm run build
```

테스트는 격리된 실제 PostgreSQL schema와 loopback fake HTTP provider를 사용합니다.
정상, rollback, ID checkpoint, worker 재시작, POST intent 이후 crash, 404/통신 실패,
상태 mapping, 메타데이터 sanitization과 기존 On-Prem 회귀를 확인합니다.
실제 GCP E2E에는 별도 운영자 승인이 필요합니다. IAM/VPC/subnet/SA/DB/Secret 또는 infra는
이 연계 코드가 변경하지 않습니다. 실제 실패 검증용 fault overlay는 일회성 외부 harness에만
두고 일반 앱 계약에는 추가하지 않습니다.
