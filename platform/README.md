# Oneport 플랫폼 · 멀티클라우드 마이그레이션 관리

온프렘 k3s에서 AWS EKS로 옮기는 앱·DB 이전을 시각화하고 보안 상태를 비교하는 관리 플랫폼입니다. 화면과 데이터 출처는 [마이그레이션·보안 대시보드](docs/MIGRATION-DASHBOARD.md)를 참고하세요. 아래 배포 SDK 설명은 이전 구조의 백엔드 기능이며 화면에서는 제외했습니다.

React 화면과 FastAPI는 공통 `DeploymentProvider`의 `plan / submit / observe`를 호출합니다. AWS(ECS Fargate), GCP(Cloud Run SDK 또는 별도 빌드 API bridge), 온프렘(K3s·Argo Rollouts) Adapter가 같은 입력·작업 ID·상태 계약을 구현하며, 실제 실행은 각 Worker가 담당합니다. Provider의 지원 기능은 다르며 미지원 요청은 거부합니다. `infra/`의 Terraform은 변경하지 않습니다.

Repository 기반 GCP Blue/Green·실패 자동 복구와 key 기반 ADC 실행 방법은 [GCP bridge 안내](docs/GCP-INTEGRATION.md)를 참고하세요. Native GCP 이미지 배포·configurable rollout·수동 promote/rollback은 별도 SDK 경로로 유지합니다.

현재는 **로컬 운영자용 개발 버전**입니다. SSO·팀별 RBAC·운영 HA를 갖춘 사내 서비스가 아닙니다. 설정이 없으면 조회용 화면만 실행되며 실제 배포를 접수하지 않습니다. 설정 등록은 클러스터 연결 검증을 의미하지 않습니다.

- [앱 만들기 → 환경 연결 → 계획 확인 → 배포: 관리 단위와 구현 범위](docs/APPLICATION-CATALOG.md)
- [공통 SDK 인터페이스와 코드 읽는 순서](docs/DEPLOYMENT-PROVIDER-INTERFACE.md)
- [시연 전용: GitHub URL 하나로 세 환경 배포하는 래퍼·화면 API](docs/DEMO-DEPLOYMENT-WRAPPER.md)
- [Shared DB·환경변수 계약](docs/SHARED-DATABASE-CONTRACT.md)
- [AWS·GCP 통합 설정](docs/CLOUD-PROVIDER-INTEGRATION.md)
- [GCP 블루그린·카나리 설정](docs/GCP-ROLLOUTS.md)
- [온프렘 접속 계약과 Worker 위치](docs/ONPREM-CONNECTION-HANDOFF.md)
- [온프렘 Helm 인수인계 — 플랫폼 팀 전달 문서](../infra/onprem/docs/helm-platform-handoff.md)
- [온프렘 UI 실제 배포·블루그린 검증 기록](docs/ONPREM-LIVE-VERIFICATION.md)
- [공개 Docker Hub 시연앱 이미지·DB 연결·EC2 이전 조건](docs/NOTES-DEMO-DEPLOYMENT.md)
- [클러스터 구성 후 첫 연결 순서](docs/FIRST-ONPREM-CONNECTION.md)
- [구조와 Provider 연결 계약](docs/ARCHITECTURE.md)
- [온프렘 인프라 전달 사항과 실검증 순서](docs/ONPREM-HANDOFF.md)

## 1. 화면 먼저 실행

Python 3.12, uv, Node 24가 필요합니다. 아래 명령은 저장소 루트 기준이며 두 터미널에서 실행합니다.

```sh
uv sync --frozen --project platform/backend
uv run --project platform/backend uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
```

```sh
cd platform/frontend
npm ci
npm run dev
```

- 화면: http://127.0.0.1:4320
- API 문서: http://127.0.0.1:8320/docs
- 환경 설정이 없으면 `preview` 모드입니다. AWS/GCP/온프렘에 리소스를 생성하지 않습니다.
- 기본 API 주소를 바꾸려면 Vite 실행 시 `ONEPORT_API_TARGET`을 지정합니다. Host·Origin 기본값은 로컬 개발용이며, 서버에서는 아래 환경변수로 실제 도메인을 등록합니다.

## 2. 테스트

Docker를 실행한 뒤 테스트 전용 DB를 만듭니다. 기존 실험 프로젝트 DB와 분리된 `oneport-platform-postgres` 컨테이너/볼륨, 로컬 55433 포트를 사용합니다.

```sh
python3 platform/scripts/local_database.py
uv run --frozen --project platform/backend ruff check platform/backend
uv run --frozen --project platform/backend ruff format --check platform/backend
uv run --frozen --project platform/backend --extra aws pytest -q platform/backend/tests
uv build --project platform/backend
cd platform/frontend
npm ci
npm run format:check
npm run build
```

테스트는 `.data/database.json` 또는 `ONEPORT_TEST_DATABASE_URL`의 실제 PostgreSQL에 임시 스키마를 만들고 지웁니다. **DB 설정이 없으면 DB 의존 테스트는 skip**되므로 통과 개수와 skip을 확인하세요. CI는 PostgreSQL 16 서비스로 모두 실행합니다. Kubernetes 전송/컨트롤러 동작은 테스트 더블이며, 테스트 통과가 팀 클러스터의 실제 배포 성공을 의미하지는 않습니다.

DB 중지는 `docker stop oneport-platform-postgres`, 재시작은 위 스크립트로 합니다. 데이터는 볼륨에 남습니다. 기존 실험 프로젝트 DB를 재사용하는 업그레이드 마이그레이션은 제공하지 않습니다.

## 3. 실제 온프렘 연결

먼저 [인프라 전달 문서](docs/ONPREM-HANDOFF.md)의 권한·이미지·네트워크 조건을 준비합니다. 예제 파일에는 실행 불가능한 이미지 digest와 접속 문자열이 들어 있습니다.

1. `platform/examples/onprem/runtime.json`을 **Git 밖의 안전한 경로**에 복사합니다. 파일 권한을 600으로 제한합니다.
2. 실제 앱/환경 ID, 운영자가 할당한 hostname, 리소스 상한, digest 이미지 allowlist, kubeconfig/context/명시적 API 주소, PostgreSQL DSN을 기입합니다. API와 Worker가 같은 설정·DB를 사용해야 합니다.
3. 비공개 레지스트리는 노드 접근 및 `image_pull_secrets`가 필요합니다. DB/Redis는 이미 준비된 불변 Secret을 논리 이름과 UID로 등록합니다. 평문 `env`는 이력 DB에 저장되므로 비밀값을 넣지 않습니다.
4. 문서의 read-only preflight를 실행하고, Worker의 private preview DNS 접근을 별도로 확인합니다.

API를 설정과 함께 재시작합니다. `ONEPORT_OPERATOR_TOKEN`은 운영자가 생성한 32자 이상의 비밀 토큰을 환경으로 제공합니다. 저장소나 공유 문서에 토큰을 넣지 않습니다.

```sh
export ONEPORT_RUNTIME_CONFIG=/safe/path/runtime.json
# ONEPORT_OPERATOR_TOKEN은 비밀 저장소 또는 로컬 셸에서 설정
uv run --project platform/backend uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
```

별도 프로세스에서 Worker를 실행합니다. Worker는 기본 HTTP 검증 모드에서 preview Service DNS와 운영 HTTPS에 접근해야 합니다. 클러스터 외부에서는 [실검증 기록](docs/ONPREM-LIVE-VERIFICATION.md)의 preview Job 모드를 사용할 수 있습니다. 이 모드도 Job 권한·probe 이미지·운영 HTTPS 접근이 필요합니다. 원격 Worker에서는 Mac의 루프백 DB 대신 접근 가능한 플랫폼 DB DSN을 사용해야 합니다.

```sh
uv run --project platform/backend python -m controlplane.execution.onprem.operator worker \
  --config /safe/path/runtime.json
```

화면의 실제 배포 메뉴에서 토큰을 입력하고 등록된 환경의 요청 JSON을 확인해 배포합니다. 토큰은 브라우저 메모리에만 유지됩니다. 큐 접수는 `202`, 실제 결과는 같은 Release ID의 이력에서 조회합니다. Worker가 없으면 요청은 큐에 남습니다.

## 구현 및 남은 범위

| 항목 | 현재 상태 |
|---|---|
| 공통 화면·환경 조회·작업 접수/조회 | 구현, API→DB→Worker 테스트 |
| 온프렘 digest 이미지 / 고정 replicas / Blue-Green | DB 없는 smoke 앱으로 최초 배포·재배포·실패 후보 복구 실검증. Notes 앱 DB 연동은 미완료 |
| Migration | 기존 DB 연결을 사용하는 Kubernetes Job, 호환 가능한 명령만 지원 |
| 실패 복구 | 이전 성공 요청 복원 및 응답 재검증. DB 스키마 자동 복구 없음 |
| 과거 Release 선택 롤백 UI/API | 후속 구현 필요 |
| GCP Git 빌드 / Cloud Run Blue/Green | 별도 provider 연계 · 후보 검증·승격·실패 자동 rollback |
| GCP PostgreSQL 발급·연결 | 운영자 설정 시 계획·배포 UI에서 전용 Cloud SQL 선택. Worker가 DB·Secret을 준비하고 Cloud Run에 연결. DB·Redis 준비 후 선택한 migration Job이 성공해야 후보 배포. Redis는 Memorystore·REDIS_URL·TLS CA 연결 |
| AWS / GCP 배포 | 공통 SDK 연결. AWS 블루그린·GCP bridge direct 실검증, native GCP rollout은 전송 대역 검증 |
| Git 빌드 / DB·Redis 신규 발급 | 공통 Git 빌드 후 이미지 배포. AWS는 앱 등록에서 RDS·ElastiCache, GCP는 전용 Cloud SQL·Memorystore, 온프렘 Helm은 별도 데이터 릴리스로 PostgreSQL·Redis 준비 |
| hostname 자동 할당, 자동 정리, HPA, Canary | 후속 구현 필요 |
| SSO, 팀별 권한/격리, 운영자 복구 UI | 운영 서비스 전환 전 필요 |

`uncertain`은 실패 확정과 다릅니다. 외부 작업 결과가 불명확한 경우 재실행을 차단하고 대조하도록 남깁니다. 이 상태를 임의로 성공/실패로 바꾸는 UI는 아직 없습니다.

## 서버 도메인 허용

Nginx 뒤에서 `platform.app.yubin.dev`로 제공할 때 API 프로세스에 다음 값을 설정합니다. 쉼표로 여러 항목을 지정할 수 있습니다. Host에는 스킴·포트를 넣지 않고 Origin에는 스킴과 필요한 포트를 포함합니다. Origin에는 끝의 `/`나 경로를 넣지 않습니다.

```sh
export ONEPORT_ALLOWED_HOSTS=platform.app.yubin.dev,127.0.0.1,localhost
export ONEPORT_ALLOWED_ORIGINS=https://platform.app.yubin.dev
```

설정 후 API를 재시작합니다. 와일드카드 `*`는 허용하지 않습니다. 환경변수를 생략하면 기존 로컬 설정을 유지합니다. CORS preflight는 등록된 Origin의 GET/POST와 Authorization·Content-Type 헤더를 허용하며, 실제 배포 API는 기존 운영자 Bearer 토큰이 필요합니다. Host·Origin 허용은 로그인이나 배포 권한을 대신하지 않습니다.

Nginx에서는 실제 Host와 Origin을 유지합니다. Origin을 삭제하거나 localhost로 바꿔 검사를 우회하지 않습니다. 기존 HTTPS server 블록의 API location 예시:

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8320;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

외부에는 HTTPS 443을 공개하고 FastAPI 8320과 DB 포트는 내부 연결로 유지합니다. 같은 VM에서 Nginx를 실행하면 API를 `127.0.0.1:8320`에 바인딩할 수 있습니다. 컨테이너에서 API를 실행하면 컨테이너 내부 `0.0.0.0:8320`으로 바인딩하고 포트 매핑 또는 사설 네트워크로 Nginx에 연결합니다. 이 PR은 실제 TLS·Nginx·EC2 인프라를 변경하지 않습니다.

## 운영 서버의 비밀값

플랫폼 DB 연결은 `database_url_file`, API 운영자 토큰은 `ONEPORT_OPERATOR_TOKEN_FILE`로 외부 비밀 파일에서 읽을 수 있습니다. 기존 직접 값과는 동시 지정하지 않습니다. AWS EC2 Role·GCP ADC·온프렘 kubeconfig의 공급 및 교체 절차는 [런타임 시크릿 관리](docs/RUNTIME-SECRETS.md)를 참고하세요.

### CPU·메모리 선택

모니터링 화면은 서비스 상태와 최신 CPU·메모리 사용률을 표시합니다. AWS는 앱의
ECS 서비스(`ClusterName`·`ServiceName`)에 대한 1분 평균, GCP는 해당 Cloud Run 서비스의
리비전별 분포를 표본 수로 가중한 1분 평균입니다. 이 값에는 서비스의 보조 컨테이너도
포함될 수 있습니다. 온프렘은 실행 중인 앱 컨테이너 합계이며 limits 대비 사용률과
실제 사용량을 함께 표시합니다. 별도의 수집기·Container Insights 활성화는 필요하지 않습니다.
측정 시각을 함께 반환하고 5분보다 오래된 값을 현재 사용량으로 제공하지 않습니다.
미수집은 0으로 표시하지 않으며, 리소스 조회 실패가 요청 지표 전체를 가리지 않습니다.
CPU·메모리 과거 그래프는 제공하지 않습니다.

새 배포는 자동 전환이 기본입니다. 지원되는 환경의 수동 승인·자동 롤백 설정은
`전환 옵션`에서 선택하며, 저장된 기존 배포 정책은 유지합니다.

초기값은 AWS `0.25 vCPU / 512 MiB`, GCP `1 vCPU / 512 MiB`, On-Premises `0.25 vCPU / 256 MiB`입니다. 이미 배포한 Helm 앱의 저장된 설정은 유지합니다. 계획 화면의 기존 선택 컨트롤은 Provider가 제공하는 지원 조합과 환경 상한에 따라 선택지를 거릅니다.

- AWS는 Fargate가 허용하는 CPU·메모리 조합만 제공합니다. 512 MiB는 0.25 vCPU에서 선택할 수 있습니다.
- GCP는 현재 전체 CPU·백그라운드 추적 실행 정책을 유지하며 512 MiB부터 256 MiB 간격으로 제공합니다. 2 vCPU는 기존 최소 1024 MiB 제한을 유지합니다.
- On-Premises는 0.1 vCPU와 128 MiB부터 선택할 수 있습니다. Helm 앱 차트의 메모리 최대값은 2048 MiB입니다.
- 모든 선택지는 등록 환경의 `max_cpu`·`max_memory_mib`로 추가 제한됩니다. CPU를 변경하면 메모리는 지원되는 조합으로 조정되고 서버에서도 다시 검증합니다.

신규 온프렘 연결 안내는 Helm을 사용합니다. Helm 차트 내부의 Argo Rollouts와 기존 등록된 Argo 앱의 호환 경로는 유지합니다.

### 시연용 토큰 생략

기본값은 운영자 토큰 인증입니다. 시연 서버는 `ONEPORT_AUTH_MODE=demo`로 토큰 없이 등록된 환경에 배포할 수 있습니다. GitHub Repository Variable `ONEPORT_AUTH_MODE=demo`를 설정하면 Platform Deploy가 명시적인 설정 파일을 배포 artifact에 생성합니다. 값은 `operator`, `demo`, `login`만 허용하며, 서버 환경변수가 있으면 그것을 우선합니다. mode가 바뀌면 같은 커밋의 수동 CD 재실행도 다른 릴리즈로 적용됩니다.

`ONEPORT_AUTH_MODE=login`은 이메일·비밀번호·팀으로 가입한 사용자의 로그인을 요구합니다.

- 가입할 때 입력한 팀 이름이 같은 사용자는 같은 팀입니다(공백·대소문자 무시). 팀 가입 승인 절차는 없습니다.
- 릴리즈와 소스 빌드에는 요청한 팀과 사용자가 기록되고, 이력에는 자기 팀의 것만 보입니다.
- 환경은 앱 하나를 실행하므로, 처음 앱을 등록하거나 배포한 팀이 그 환경을 소유합니다. 다른 팀에는 그 환경의 앱 정보·로그·지표·trace가 보이지 않고, 다른 팀은 그 환경에 배포할 수 없습니다.
- 팀 안의 역할은 관리자(`owner`), 배포 가능(`member`), 보기 전용(`viewer`)입니다. 팀을 처음 만든 사용자가 관리자이고, 이후 가입자는 보기 전용으로 시작합니다. 관리자가 「팀」 화면에서 권한을 바꿉니다. 보기 전용은 앱 등록·배포·수동 전환·빌드가 403으로 거절됩니다. 팀에는 관리자가 한 명 이상 남아야 합니다.
- login 모드 이전(또는 operator·demo 모드)에 만든 릴리즈는 팀이 없어 어느 팀에도 보이지 않습니다.
- 비밀번호는 scrypt 해시로, 세션은 토큰의 SHA-256으로만 저장합니다. 세션 쿠키는 `HttpOnly`·`SameSite=Lax`이고 7일 동안 유효합니다. 사용할 때마다 만료가 다시 7일로 늘어나므로, 일주일 안에 다시 접속하면 로그인을 반복하지 않습니다. API는 확인한 세션을 30초 동안 재사용해 화면의 주기적 조회마다 DB를 읽지 않습니다.

화면은 `/api/v1/access` 결과에 따라 데모 모드에서 토큰 입력칸을 숨기고 배포 이력을 바로 조회합니다. 데모 모드의 변경 요청은 허용 Origin이 필요하며 Host·앱/환경·이미지·자원 제한은 그대로 적용됩니다. 공개 데모는 로그인 없이 배포·승격·롤백을 실행할 수 있으므로 공유 운영 환경의 사용자 권한 관리 용도로 쓰지 않습니다. 시연 종료 후 변수 값을 `operator`로 되돌리고 CD를 실행하면 토큰 인증을 복원할 수 있습니다.
