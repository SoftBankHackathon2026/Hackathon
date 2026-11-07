# Oneport 마이그레이션 관리 플랫폼

온프렘 k3s에서 AWS EKS로 앱과 DB를 옮기는 과정을 보여주는 관리 플랫폼이에요. 실제 전환은 이 레포의 [`migration`](../migration) 도구(Debezium CDC, cutover 상태 기계, OPA 게이트, verify)가 하고, 플랫폼은 그 결과를 읽어서 보여줘요. 플랫폼은 클러스터와 DB에 쓰지 않아요.

| 화면 | 내용 |
|---|---|
| `#migrations` | 이전 작업 목록, 작업별 단계와 쓰기량 |
| `#migrations/<job_id>` | 전환 타임라인, 환경별 쓰기, 쓰기 중단 구간, 정책 게이트, 데이터 검증 |
| `#environments` | 온프렘·AWS·GCP 아키텍처 캔버스(2D/3D), 구성 요소 상태 |
| `#security` | 환경별 준수율, 도착 환경별 전환 전후 비교, 표준별 준수(CIS GCP 포함), 보안 문제 목록 |

- 백엔드: `backend/` (FastAPI, Python 3.12, uv)
- 프론트: `frontend/` (React 19, Vite 7). 디자인 규칙은 [frontend/DESIGN.md](frontend/DESIGN.md)
- 화면과 데이터 출처 상세: [docs/MIGRATION-DASHBOARD.md](docs/MIGRATION-DASHBOARD.md)
- 비용 추산(환경별 월 비용·이전 증감): [docs/COST-ESTIMATION.md](docs/COST-ESTIMATION.md)

## 1. 시뮬레이션으로 켜기

설정이 없으면 온프렘·AWS·GCP 세 환경의 시뮬레이션 데이터로 떠요. DB도 클라우드 리소스도 필요 없어요. 화면 상단에 "시뮬레이션 데이터"가 표시돼요.

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

## 2. 실제 환경에 연결하기 (AWS에 띄우고 온프렘·AWS·GCP 연결)

플랫폼은 AWS EKS `oneport` 네임스페이스에 띄우고, 세 환경을 읽기 전용으로 연결해요. 데이터는 세 경로로 들어와 플랫폼 DB에 쌓여요.

```text
                       ┌──────────── AWS EKS (oneport 네임스페이스) ────────────┐
 화면 ── port-forward ─▶ │ oneport-web ─▶ oneport-api ─▶ 플랫폼 DB (RDS 안 별도 DB)  │
                       │                     ▲            ▲                       │
                       │       oneport-collector ─────────┘                       │
                       └───────┬──────────────┬───────────────┬──────────────────┘
       ServiceAccount (EKS)    │   Tailscale  │   공개 엔드포인트 │
                               ▼              ▼                 ▼
                      EKS kube API   온프렘 kube API·워커·Connect   GKE kube API
                                     (172.30.1.10·.7, 상태 프록시)
 온프렘 워커 Pod 의 보고기 ── Tailscale ──▶ (내부 Ingress, 선택) ──▶ POST /api/v1/ingest/...
```

| 데이터 | 방식 | 주기 |
|---|---|---|
| 쓰기 워커 `/status`, Kafka Connect 커넥터 상태 | 수집기 pull | 5초 |
| 각 클러스터 kube API (구성 요소·호출 관계, 보안 판정, Argo CD 앱) | 수집기 pull, 읽기 전용 | 1분 |
| cutover 단계, OPA 판정, verify 결과 | 워커 Pod 보고기 push | 5초 |

외부 연동에 클라우드 SDK를 새로 붙일 필요는 없어요. 수집기는 HTTP와 Kubernetes Python 클라이언트로 읽기만 하고, 보고기는 표준 라이브러리만 써요. 백엔드에 있는 클라우드별 배포 SDK(`DeploymentProvider`)는 이전 구조의 배포 기능이고 이 화면에서는 쓰지 않아요([docs/DEPLOYMENT-SDK.md](docs/DEPLOYMENT-SDK.md)).

배포에 쓰는 파일은 [`deploy/`](deploy)에 있어요.

| 폴더 | 내용 |
|---|---|
| [`deploy/aws`](deploy/aws) | EKS용 kustomization. API·수집기·화면, 읽기 RBAC, 환경 설정(`fleet.json`). 내부 ALB Ingress(`ingress.yaml`)는 월 비용이 들어 기본에서 빠져 있어요. 보고기가 플랫폼에 push 해야 할 때 `kustomize edit add resource ingress.yaml`로 더해요 |
| [`deploy/onprem`](deploy/onprem) | gitops에 넣을 보고기 sidecar patch와 보고기 스크립트(`fleet_reporter.py`)·ConfigMap. 워커·Connect 상태는 gitops `apps/platform-status-proxy` |
| [`deploy/reader`](deploy/reader) | 플랫폼 밖 클러스터(GKE, 온프렘)에 두는 읽기 전용 계정과 kubeconfig 생성 스크립트 |

### 2-1. 환경별로 먼저 준비할 것

**AWS (플랫폼이 뜨는 곳)**

| 준비물 | 방법 |
|---|---|
| 이미지 | 팀 계정 ECR `ghost/oneport-platform-api`, `ghost/oneport-platform-web`(태그 변경 불가, 푸시 때 스캔)을 써요. EKS 노드 역할이 ECR 읽기 권한을 가져 pull secret 이 필요 없어요. 아래 "이미지 올리기"로 커밋 해시 태그를 올려요. ghcr 패키지는 조직 정책상 공개할 수 없어 EKS 에서 쓰지 않아요 |
| 플랫폼 DB | RDS `oneaction-review-db` 안에 `oneport_platform` 데이터베이스와 전용 사용자를 만들어요. 앱 DB와 섞지 않아요. 스키마는 API가 처음 뜰 때 만들어요 |
| EKS 읽기 권한 | 필요 없어요. 수집기가 EKS 안에서 ServiceAccount `oneport-collector`(`deploy/aws/rbac.yaml`)로 읽어요. access entry는 쓰지 않아요 |
| 온프렘으로 가는 경로 | oneaction-infra `infra/tailscale`의 `tailnet_routes`(`SoftBankHackathon2026/oneaction-infra#5`). 라우터가 `--accept-routes`로 홈랩 경로를 받고, VPC 라우팅 테이블은 k3s 노드 두 대(`/32`)만 라우터로 보내요 |

**온프렘 k3s**

| 준비물 | 방법 |
|---|---|
| 읽기 kubeconfig | [`infra/install/onprem-k8s/generate-kubeconfig.sh`](../infra/install/onprem-k8s)의 `hackathon-user`(`view` + argoproj 읽기)를 써요. API 주소는 `https://172.30.1.10:6443` |
| 노드 정보 | `hackathon-user`는 노드 목록을 못 읽어서 클러스터 정보의 노드별 할당 가능량·상태가 비어요. 노드 이름·사용량·요청량은 metrics 와 파드 목록으로 채워요. 채우려면 `hackathon-user`에 `nodes` list 를 더해요 |
| 워커·Connect 상태 | gitops `apps/platform-status-proxy` (`SoftBankHackathon2026/gitops#19`). nginx 프록시가 `GET /worker/status`, `GET /connect/connectors/<job>-(source\|sink)/status`만 넘겨요. 워커 제어 API(`/pause`, `/target` 등)와 Kafka Connect REST는 열지 않아요. NodePort `30880`, NetworkPolicy는 LAN(노드 IP 제외)만 허용 |
| 보고기 | 마이그레이션 담당 작업이에요. 보고기가 닿을 플랫폼 주소(내부 Ingress 등)를 정한 뒤 `deploy/onprem/reporter-sidecar.patch.yaml`을 gitops `apps/db-insert-worker`에 patch로 붙이고, `oneport-fleet-reporter` ConfigMap과 `oneport-ingest` Secret을 만들어요 (`deploy/onprem/kustomization.yaml` 주석) |
| AWS VPC 경로 | k3s 노드가 Tailscale `--accept-routes`로 `10.0.0.0/16`을 받아요 (설정 완료). LAN 우회 규칙 `tailscale-lan-bypass.service`가 노드 간 통신을 지켜요 |

**GCP GKE** (oneaction-infra `infra/gcp`, PR #3)

| 준비물 | 방법 |
|---|---|
| 읽기 계정 | GKE에 `deploy/reader/reader.yaml`을 적용하고 `deploy/reader/make-kubeconfig.sh oneaction-gke gcp.kubeconfig`로 kubeconfig를 만들어요. ClusterRole 을 만들므로 적용하는 사람에게 `roles/container.admin`이 필요해요(`roles/container.clusterAdmin`은 클러스터 자원 관리 권한이라 RBAC 를 못 만들어요) |
| API 접근 | GKE 엔드포인트는 공개 + 승인 네트워크 방식이에요. `public_access_cidrs`에 AWS NAT 공인 IP(`terraform -chdir=infra/network output`)를 넣어요 |
| 이전 작업 | `migration` 도구는 아직 GCP 대상을 지원하지 않아요. GCP는 아키텍처·보안 화면에 나오고, GCP로 가는 이전 작업은 도구가 지원한 뒤 `migrations[]`에 추가해요 |

### 2-2. 설정 값 채우기

[`deploy/aws/fleet.json`](deploy/aws/fleet.json)은 지금 Terraform·gitops 기준 값으로 채워져 있어요. 바뀌는 값만 고쳐요.

| 필드 | 값 |
|---|---|
| `environments[].in_cluster` | 플랫폼이 그 클러스터 안에서 돌면 `true` (EKS). 나머지는 `kubeconfig`·`context` |
| `environments[].kubeconfig` | `/etc/oneport/kube/onprem.kubeconfig`, `/etc/oneport/kube/gcp.kubeconfig` (Secret에서 마운트) |
| `environments[].components` | 클러스터 안 구성 요소는 수집기가 워크로드 목록(Deployment·StatefulSet·DaemonSet·Rollout)에서 찾고, Service·Ingress·ConfigMap 에 적힌 주소로 호출 관계를 이어요. 이 필드에는 Kubernetes 로 찾을 수 없는 클러스터 밖 구성 요소(관리형 DB·로드 밸런서·Tailscale)만 적어요. 비우면 같은 `id`(onprem·aws·gcp)의 기본값을 써요 |
| `environments[].components[].endpoint_hints` | 클러스터 밖 구성 요소의 주소 조각(예: RDS 식별자 뒤 `.`). 워크로드 설정 값에 이 조각이 있으면 그 워크로드와 선으로 이어요 |
| `environments[].cluster_path` | 클러스터 안 구성 요소를 그릴 위치(VPC › 서브넷 › 클러스터). 비우면 기본값을 써요 |
| `environments[].data_services`, `boundary` | 화면에 그릴 이름. Terraform 출력과 맞춰요 |
| `environments[].declared` | kube API로 알 수 없는 클라우드 보안 사실. Terraform에 있는 그대로 `true`/`false`, 모르면 빼요(미확인) |
| `migrations[].worker_url`, `connect_url` | 온프렘 읽기 전용 상태 프록시. 두 노드 주소를 쉼표로 적어요(`http://172.30.1.10:30880/worker,http://172.30.1.7:30880/worker`, Connect는 `/connect`). 프록시 NodePort가 `externalTrafficPolicy: Local`이라 프록시 파드가 있는 노드만 응답하고, 수집기는 응답하는 주소로 넘어가 다음부터 그 주소를 먼저 써요 |

[`deploy/aws/settings.env`](deploy/aws/settings.env)는 port-forward 접속 기준(`localhost`)이에요. Ingress 를 더하면 그 주소를 `ONEPORT_ALLOWED_HOSTS`·`ONEPORT_ALLOWED_ORIGINS`에 넣어요.

### 2-3. AWS에 띄우기

**이미지 올리기** (커밋 해시 태그, EKS 노드와 같은 linux/amd64)

```sh
REG=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.ap-northeast-2.amazonaws.com
SHA=$(git rev-parse HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin "$REG"
docker buildx build --platform linux/amd64 -t "$REG/ghost/oneport-platform-api:$SHA" --push platform/backend
docker buildx build --platform linux/amd64 -t "$REG/ghost/oneport-platform-web:$SHA" --push platform/frontend
```

**배포**. 이미지 주소에는 계정 번호가 들어가므로 저장소에 적지 않고, 배포할 때 kustomize 로 바꿔요.

```sh
umask 077
kubectl apply -f platform/deploy/aws/namespace.yaml
kubectl -n oneport create secret generic oneport-secrets \
  --from-file=database-url=./database-url \
  --from-file=ingest-token=./ingest-token \
  --from-file=onprem.kubeconfig=./onprem.kubeconfig
cp -R platform/deploy/aws /tmp/oneport-deploy && cd /tmp/oneport-deploy
kustomize edit set image \
  "ghcr.io/softbankhackathon2026/oneport-platform-api=$REG/ghost/oneport-platform-api:$SHA" \
  "ghcr.io/softbankhackathon2026/oneport-platform-web=$REG/ghost/oneport-platform-web:$SHA"
kubectl apply -k .
kubectl -n oneport port-forward svc/oneport-web 18080:8080
```

화면은 http://localhost:18080 이에요. 비밀값 파일은 끝에 줄바꿈 없이 한 줄로 만들어요(`printf '%s'`). 줄바꿈이 들어가면 DB 비밀번호가 맞지 않아요.

- `database-url`은 DSN 한 줄, `ingest-token`은 32자 이상이에요. GKE 를 붙일 때 `gcp.kubeconfig`를 더해요. 없으면 그 환경만 "연결하지 못했어요"로 보여요.
- 플랫폼 DB 는 기존 RDS 안의 `oneport_platform` 데이터베이스와 같은 이름의 전용 사용자예요(슈퍼유저 아님, `PUBLIC` 접속 권한 회수). 새 RDS 를 만들지 않아요.
- API 는 배포 Provider 설정 없이 `ONEPORT_DATABASE_URL_FILE`만으로 실측 모드가 돼요. 배포 기능을 안 쓰므로 운영자 토큰도 필요 없어요.
- 온프렘(`172.30.1.10`)은 VPC → 홈랩 경로(oneaction-infra `infra/tailscale` 의 `tailnet_routes`, 라우터 `--accept-routes`)가 있어야 읽혀요.

로컬에서 같은 구성을 띄울 때는 이렇게 해요.

```sh
export ONEPORT_DATABASE_URL_FILE=/safe/path/database-url
export ONEPORT_FLEET_CONFIG=/safe/path/fleet.json
export ONEPORT_INGEST_TOKEN_FILE=/safe/path/ingest-token
cd platform/backend
uv run --frozen uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
uv run --frozen python -m controlplane.execution.fleet_collector      # 별도 프로세스, --once 로 한 번만
```

### 2-4. 확인

| 확인 | 방법 |
|---|---|
| 실측 모드 | `kubectl -n oneport port-forward svc/oneport-web 18080:8080` 뒤 `curl -H "Host: localhost" http://127.0.0.1:18080/api/v1/fleet` 의 `source` 가 `live` |
| ingest 인증 | 토큰 없이 `POST /api/v1/ingest/migrations/<job_id>` → 401, 토큰과 함께 → 202 |
| 수집 | `kubectl -n oneport logs deploy/oneport-collector`에 `WARNING ... error=` 가 없어요. 닿지 않는 환경만 경고가 남고 나머지는 계속 수집해요 |
| 보안 판정 | `#security`에서 환경별 "미확인"이 줄고, 출발 환경과 각 도착 환경(AWS, GCP)의 비교 카드가 보여요 |

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
