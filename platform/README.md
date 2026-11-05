# Oneport 마이그레이션 관리 플랫폼

온프렘 k3s에서 AWS EKS로 앱과 DB를 옮기는 과정을 보여주는 관리 플랫폼이에요. 실제 전환은 이 레포의 [`migration`](../migration) 도구(Debezium CDC, cutover 상태 기계, OPA 게이트, verify)가 하고, 플랫폼은 그 결과를 읽어서 보여줘요. 플랫폼은 클러스터와 DB에 쓰지 않아요.

| 화면 | 내용 |
|---|---|
| `#migrations` | 이전 작업 목록, 작업별 단계와 쓰기량 |
| `#migrations/<job_id>` | 전환 타임라인, 환경별 쓰기, 쓰기 중단 구간, 정책 게이트, 데이터 검증 |
| `#environments` | 환경별 아키텍처 캔버스(2D/3D), 구성 요소 상태 |
| `#security` | 환경별 준수율, 전환 전후 비교, 표준별 준수, 보안 문제 목록 |

- 백엔드: `backend/` (FastAPI, Python 3.12, uv)
- 프론트: `frontend/` (React 19, Vite 7). 디자인 규칙은 [frontend/DESIGN.md](frontend/DESIGN.md)
- 화면과 데이터 출처 상세: [docs/MIGRATION-DASHBOARD.md](docs/MIGRATION-DASHBOARD.md)

## 1. 시뮬레이션으로 켜기

설정이 없으면 시뮬레이션 데이터로 떠요. DB도 클라우드 리소스도 필요 없어요. 화면 상단에 "시뮬레이션 데이터"가 표시돼요.

필요한 도구: Python 3.12, [uv](https://docs.astral.sh/uv/), Node 24

```sh
cd platform/backend
uv sync --frozen --extra aws
ONEPORT_AUTH_MODE=demo uv run --frozen --extra aws uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
```

```sh
cd platform/frontend
npm ci
npm run dev
```

- 화면: http://127.0.0.1:4320, API 문서: http://127.0.0.1:8320/docs
- `ONEPORT_AUTH_MODE=demo`는 로그인 없이 보는 시연 모드예요. 빼면 가입·로그인이 필요해요.
- API 주소를 바꾸려면 `ONEPORT_API_TARGET=http://127.0.0.1:<port> npm run dev`로 띄워요.

## 2. 실제 환경에 연결하기 (실측 모드)

실측 모드에서는 세 경로로 데이터가 들어와요. 셋 다 플랫폼 DB(Postgres)에 쌓이고, 화면은 그 DB를 읽어요.

```text
수집기 (pull, 5초)   ── 쓰기 워커 /status, Kafka Connect 커넥터 상태 ─┐
수집기 (pull, 1분)   ── 각 클러스터 kube API 읽기 전용 (보안 판정용) ──┼─▶ 플랫폼 DB ─▶ API ─▶ 화면
보고기 (push, 5초)   ── 워커 PVC 의 cutover·OPA·verify 결과 ─────────┘
```

외부 연동에 클라우드 SDK를 새로 붙일 필요는 없어요. 수집기는 HTTP와 Kubernetes Python 클라이언트로 읽기만 하고, 보고기는 표준 라이브러리만 써요. 백엔드에 있는 클라우드별 배포 SDK(`DeploymentProvider`)는 이전 구조의 배포 기능이고 이 화면에서는 쓰지 않아요([docs/DEPLOYMENT-SDK.md](docs/DEPLOYMENT-SDK.md)).

### 2-1. 준비할 것

| 준비물 | 어디서 | 비고 |
|---|---|---|
| 플랫폼 DB (PostgreSQL 16) | 플랫폼 전용 DB를 하나 준비 | 이전 대상 앱 DB와 분리해요. 스키마는 API가 처음 뜰 때 만들어요 |
| 온프렘 kubeconfig | [`infra/install/onprem-k8s/generate-kubeconfig.sh`](../infra/install/onprem-k8s) | `hackathon-user`(`view` + argoproj 읽기) 읽기 전용 |
| EKS kubeconfig | `aws eks update-kubeconfig --name oneaction` | EKS에 `AmazonEKSViewPolicy` access entry를 가진 역할이 필요해요. oneaction-infra `eks` 스택에는 아직 관리자 entry만 있어요 |
| 워커·Connect 주소 | gitops `apps/db-insert-worker`, `apps/migration-cdc` 의 Service | 클러스터 내부 주소예요. 수집기가 닿는 곳에서 실행해요 |
| 클라우드 보안 서비스 여부 | oneaction-infra Terraform 기준 | GuardDuty·Security Hub·WAF 등. 아래 `declared`에 적어요 |
| ingest 토큰 | 직접 생성 (32자 이상) | 보고기와 API가 같은 값을 써요 |

### 2-2. 설정 파일 두 개 만들기

설정 파일은 비밀값이 들어가므로 **Git 밖**에 두고 권한을 600으로 해요.

**① 플랫폼 DB 설정**: [`examples/onprem/runtime.json`](examples/onprem/runtime.json)을 복사해서 `database_url`만 바꿔요. 나머지 필드는 이전 배포 기능용이라 대시보드만 쓸 때는 예시 값 그대로 둬도 돼요. DSN을 파일로 분리하려면 `database_url` 대신 `database_url_file`을 써요.

```json
{ "database_url": "postgresql://<user>:<password>@<host>:5432/<db>", "...": "예시 파일의 나머지 필드" }
```

**② 환경·작업 설정**: [`examples/fleet/fleet.live.json`](examples/fleet/fleet.live.json)을 복사해서 실제 값으로 바꿔요.

| 필드 | 값 |
|---|---|
| `mode` | `live` |
| `environments[].kubeconfig`, `context` | 2-1의 kubeconfig 경로와 context 이름 |
| `environments[].app_namespace`, `argocd_namespace` | 온프렘 `hackathon`, EKS `platform` / 둘 다 `argocd` |
| `environments[].data_services` | 화면에 그릴 DB·Kafka·스토리지 이름. RDS·MSK 이름은 Terraform 출력과 맞춰요 |
| `environments[].declared` | kube API로 알 수 없는 클라우드 보안 사실. Terraform에 있는 그대로 `true`/`false` |
| `migrations[].job_id` | `migration` 도구의 작업 ID (`forward_001`, `return_001` 등) |
| `migrations[].source_env`, `target_env` | 위 `environments[].id` |
| `migrations[].worker_url`, `connect_url` | 쓰기 워커 `/status`, Kafka Connect REST 주소 |
| `sample_seconds` | 수집 주기 (기본 5) |

### 2-3. 실행

```sh
export ONEPORT_RUNTIME_CONFIG=/safe/path/runtime.json      # ① 플랫폼 DB
export ONEPORT_FLEET_CONFIG=/safe/path/fleet.live.json     # ② 환경·작업
export ONEPORT_INGEST_TOKEN_FILE=/safe/path/ingest-token   # 보고기 push 인증

cd platform/backend
uv run --frozen --extra aws uvicorn controlplane.main:app --host 0.0.0.0 --port 8320   # API
uv run --frozen --extra aws python -m controlplane.execution.fleet_collector           # 수집기 (별도 프로세스)
```

- `GET /api/v1/fleet` 응답의 `source`가 `live`면 실측 모드예요. 화면의 "시뮬레이션 데이터" 표시가 사라져요.
- 수집기는 워커·Connect 주소와 두 kube API에 닿는 곳에서 실행해요. 온프렘 k3s 안의 Pod로 띄우거나, Tailscale로 붙은 서버에서 실행해요.
- 닿지 않는 대상은 `WARNING ... error=ConnectError`만 남기고 계속 돌아요. 한 번만 돌려 보려면 `--once`를 붙여요.

### 2-4. 보고기 붙이기

cutover 단계, OPA 판정, verify 결과는 워커 PVC의 SQLite에 있어서 플랫폼이 직접 읽지 못해요. 워커 Pod에 [`ops/fleet_reporter.py`](ops/fleet_reporter.py)를 sidecar로 붙여 같은 PVC를 읽고 플랫폼으로 보내요.

```sh
ONEPORT_PLATFORM_URL=https://<플랫폼 API 주소> ONEPORT_INGEST_TOKEN=<토큰> \
  python fleet_reporter.py --job-id forward_001 --state-dir /var/lib/worker --interval 5
```

워커 Pod에서 플랫폼 API 주소로 나갈 수 있어야 해요. 토큰은 Secret으로 넣어요.

### 2-5. 확인

| 확인 | 방법 |
|---|---|
| 실측 모드 | `curl <API>/api/v1/fleet` 의 `source` 가 `live` |
| ingest 인증 | 토큰 없이 `POST /api/v1/ingest/migrations/<job_id>` → 401, 토큰과 함께 → 202 |
| 워커·커넥터 수집 | 수집기 로그에 해당 작업의 `WARNING`이 없고, 작업 상세의 쓰기 차트가 움직임 |
| 보안 판정 | `#security`에서 `unknown`이 줄어듦 (kube API 읽기 성공) |

## 3. 테스트

백엔드 전체 테스트는 Docker로 띄운 로컬 Postgres가 필요해요.

```sh
python3 platform/scripts/local_database.py
cd platform/backend
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen --extra aws pytest -q tests
```

```sh
cd platform/frontend
npm run format:check && npm test && npm run build
```

## 4. 이 레포로 옮기면서 뺀 것

원본 레포의 `infra/`(플랫폼 서버용 Terraform, 온프렘 Helm 차트 원본)와 배포 CI는 옮기지 않았어요. 플랫폼은 그것 없이 돌아가요.

- 온프렘 배포용 Helm 차트는 패키지로 [`vendor/helm`](vendor/helm)에 들어 있어요.
- AWS 쪽 인프라(EKS, RDS, MSK)는 `SoftBankHackathon2026/oneaction-infra`가 관리해요. 플랫폼은 그 결과를 kube API와 설정 파일로 읽기만 해요.
- 일부 이전 문서(`docs/`)는 원본 레포의 `infra/` 경로를 가리켜요. 배포 SDK 관련 문서라 대시보드 실행에는 영향이 없어요.
