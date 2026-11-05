# AWS·GCP·온프렘 통합 작업 기록

## 구조

```text
React → FastAPI → ProviderRegistry → Provider별 입력 검증
                  │
                  └→ PostgreSQL ReleaseStore
                       ├→ AWS Worker → boto3 → ECS Fargate
                       ├→ GCP bridge Worker → localhost GCP API → GCP SDK
                       └→ Onprem Worker → Kubernetes client → Argo Rollouts
```

공통 SDK는 `DeploymentSpec → DeploymentOperation → DeploymentStatus`와 `validate / plan / submit / observe`를 제공합니다. API와 배포 서비스는 Provider별로 분기하지 않습니다. 각 Adapter 내부에서 기존 Worker 계약으로 변환합니다. GCP가 지원하는 입력은 아직 저장소 방식뿐이므로 공통 인터페이스가 같아도 기능 범위는 다릅니다. 지원되지 않는 필드는 조용히 무시하지 않고 거부합니다. 자세한 호출 흐름은 [공통 SDK 인터페이스](DEPLOYMENT-PROVIDER-INTERFACE.md)를 참고하세요.

DB 시연 계약과 미구현 범위는 [Shared DB 계약](SHARED-DATABASE-CONTRACT.md)을 기준으로 합니다.

## 가져온 코드와 변경 범위

- AWS: `softbank-hackathon-2026-term1-azalea/aws-deployment-lab`, `73d8dad971c1d75fff884221afef4e1c9a802068`. `platform/backend/src/oneport_aws`에 SDK·Worker를 이식했습니다. 기존 공통 controlplane 패키지 의존성을 현재 모델·저장소와 순수 task/candidate 모듈로 교체했습니다. 원본 테스트도 `tests/aws`로 옮겼습니다.
- GCP: `softbank-hackathon-2026-term1-azalea/gcp-deployment-lab`. 최초 연결은 `feature/gcp-fastapi-deployment`의 `428525f1ebacfd0aca838dc403b2b7c94c2a9204` 기준입니다. 실제 배포 검증은 `feature/gcp-bluegreen-rollback`의 `b2e7e40` + immutable 태그 수정 `3c1176f`로 진행했습니다. 수정은 [GCP PR #1](https://github.com/softbank-hackathon-2026-term1-azalea/gcp-deployment-lab/pull/1)에 있으며 아직 병합되지 않았습니다. SDK를 중복 복사하지 않고 별도 API에 HTTP로 연결하므로 해당 수정은 별도 GCP API에도 적용해야 합니다.
- `infra/gcp/integration`은 별도의 인프라 검증 도구입니다. DB 생성 예제가 있다고 현재 GCP 플랫폼 API의 지원 기능으로 간주하지 않습니다.

AWS 앱 등록은 공통 플랫폼 DB에 저장합니다. 등록 변경과 배포 접수는 잠금으로 직렬화하며 진행 중·uncertain 릴리즈가 있으면 설정 변경을 거부합니다. Shared 웹 등록은 새 RDS를 만들지 않습니다. Dedicated·캐시를 선택하면 AWS Worker가 백그라운드로 RDS·ElastiCache Serverless를 만듭니다. 캐시를 쓰려면 `.env`에 `AWS_CACHE_SUBNET_IDS`, `AWS_CACHE_SECURITY_GROUP_ID`, `ONEPORT_CACHE_MAX_STORAGE_GB`, `ONEPORT_CACHE_MAX_ECPU`(infra/aws/data-access.md)가 있어야 합니다.

GCP는 `demo` 기존 서비스만 변경하며 Git 기본 HEAD를 빌드합니다. 현재 공통 bridge는 기본 direct 배포를 요청하며 팀 API의 blue/green·rollback API를 노출하지 않습니다. 해당 API의 이력은 프로세스 메모리에 있으므로 작업 ID를 플랫폼 DB에 저장하고 조회합니다. 접수 응답을 잃거나 GCP 재시작으로 이력을 잃으면 `uncertain`으로 멈추며 POST를 자동 재전송하지 않습니다. API 성공은 공개 서비스 접속을 보장하지 않습니다.

## 운영자 설정

기존 `ONEPORT_RUNTIME_CONFIG`는 온프렘 전용 호환 경로입니다. 여러 Provider는 `ONEPORT_CONFIG`에 아래 JSON 파일 경로를 지정합니다. 둘을 동시에 지정할 수 없습니다.

```json
{
  "database_url": "postgresql://<platform-user>:<password>@127.0.0.1:55433/<platform-db>",
  "providers": [
    {"runtime": "aws_ecs", "config_file": "aws-runtime.json"},
    {"runtime": "gcp_cloud_run", "config_file": "gcp-runtime.json"},
    {"runtime": "onprem_argo", "config_file": "onprem-runtime.json"}
  ]
}
```

연결할 환경만 providers에 넣습니다. 각 Worker의 database_url은 위 플랫폼 이력 DB와 동일해야 합니다. 이것은 사용자 앱의 DATABASE_URL과 다릅니다. 설정 파일은 Git 외부에서 관리합니다. API는 32자 이상 `ONEPORT_OPERATOR_TOKEN`이 필요합니다.

AWS 설치: `uv sync --project platform/backend --extra aws --frozen`.
AWS runtime은 기존 `OneportEcsSettings` 설정을 사용하며 앱별 `secret_refs.db`는 Shared DB 접속 Secret의 고정 버전 ARN으로 준비합니다. `env_file`은 API에서 runtime 파일 기준으로 해석합니다. 독립 Worker CLI에는 절대 경로를 권장합니다. 플랫폼 코드만 설치하면 AWS SDK는 선택 의존성입니다.

GCP runtime 예시:

```json
{
  "application": "demo",
  "environment_id": "gcp-demo",
  "database_url": "<공통 플랫폼 PostgreSQL DSN>",
  "upstream_url": "http://127.0.0.1:8740",
  "service_url": "https://<실제-Cloud-Run-서비스-주소>",
  "allowed_repositories": ["https://github.com/<owner>/<repo>"]
}
```

GCP 팀 API는 인증 없는 서비스이므로 loopback에만 실행합니다. GCP API 실행 호스트에 Docker, GCP 설정 및 해당 팀 API가 요구하는 서비스 계정 자격 증명이 필요합니다. bridge 설정에는 클라우드 키를 넣지 않습니다.

각 Worker는 별도 프로세스입니다:

```bash
uv run --project platform/backend --extra aws python -m oneport_aws.execution.oneport_ecs_operator worker --config /absolute/path/aws-runtime.json
uv run --project platform/backend python -m controlplane.execution.gcp_bridge.operator worker --config /absolute/path/gcp-runtime.json
```

Frontend는 localhost:4320을 사용합니다. 기본 API의 Origin 허용 목록도 이 주소를 기준으로 합니다. 실행 설정이 없으면 preview로 시작하며 실제 배포 버튼을 활성화하지 않습니다.

## 검증 범위

실제 로컬 PostgreSQL의 격리 스키마와 AWS fake/HTTP MockTransport를 사용합니다. Shared 참조 재사용, 누락 참조 거부, AWS Dedicated·캐시의 백그라운드 생성·주입·삭제 순서·응답 유실 시 재실행 금지, 다른 Provider의 Dedicated 거부, 접수 중복 방지, GCP 응답 유실·이력 유실·다른 앱/URL 응답 거부를 검증합니다. 실제 AWS·GCP 배포나 Shared DB 연결 검증은 이 로컬 테스트에 포함되지 않습니다.

```bash
uv run --project platform/backend --extra aws pytest platform/backend/tests -q
uv run --project platform/backend --extra aws ruff check platform/backend
npm --prefix platform/frontend run build
```

## 인프라 문서와 현재 플랫폼 지원 범위

- ECS Fargate + 앱별 ALB host rule + blue/green target group 구조를 사용한다. 트래픽 전환은 ECS infrastructure role이 담당한다.
- 이미지 pull·로그 전송·task definition Secret 주입은 task execution role의 역할이다. 컨테이너 안의 AWS API 호출은 앱 runtime role을 사용한다.
- DNS는 도메인을 ALB 주소로 해석한다. HTTP 요청이 Route 53 서버를 프록시처럼 거치는 것은 아니다.
- 인프라 문서의 앱별 RDS 생성은 Dedicated에 해당한다. 공개 웹 API 기본값은 Shared Secret 연결이고, AWS는 Dedicated를 선택할 수 있다.
- BLUE_GREEN/CANARY는 AWS SDK 경로에 구현돼 있다. LINEAR, 운영 중 수동 스케일 API, 자동 스케일 정책, 공통 과거 릴리즈 선택/복원 API는 후속 작업이다. 요청의 replicas는 배포 시 desiredCount에 반영된다.
- AWS 캐시는 infra/aws/data-access.md §4 절차(사용자·`default` 비활성 사용자·user group → Valkey 캐시 → 비밀번호 재설정과 `REDIS_URL` 저장)를 따른다. 실제 AWS 생성 검증은 별도로 기록한다.
- RunTask가 허용되지 않아 AWS Migration은 비활성화한다. GCP는 기존 서비스 이미지 갱신 범위이며 신규 리소스 발급/마이그레이션을 지원한다고 표시하지 않는다.
- 실제 AWS 최초 배포·UI 블루그린 재배포 및 GCP UI direct 배포 결과는 [검증 기록](LIVE-DEPLOYMENT-TEST.md)에 기록했다. AWS는 HTTPS 200, GCP는 인증 요청 200/비인증 요청 403을 확인했다. AWS 임시 실행 자원은 정리했고 GCP 기존 이미지·트래픽 설정은 복원했다.
- 온프렘의 전달된 namespace·context·API 및 Worker 배치 조건은 [온프렘 접속 계약](ONPREM-CONNECTION-HANDOFF.md)을 참고한다. 실제 kubeconfig 파일 경로와 내부 preview 접근은 확인 대기 중이다.

공식 참고: [ECS ALB blue/green 구성](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/alb-resources-for-blue-green.html).

## 최신 main 병합 — GCP 두 실행 경로

PR #19의 native GCP rollout 코드를 공통 SDK로 연결했다. 기존 검증 경로는 `gcp_bridge` 모듈로 보존했으며, `providers/gcp`와 `execution/gcp`는 Google SDK 직접 제어 경로다. `engine=cloud_run`이면 native를 사용하고, 기존 `upstream_url` 설정은 bridge를 사용한다. 한 플랫폼 인스턴스에서는 두 GCP 계약 중 하나를 선택한다.

Native 경로는 digest 이미지, 카나리 비율·대기·수동/자동 승격, bake, 자동 복귀와 진행 중 수동 복귀를 지원한다. [설정과 내부 후보 접근 조건](GCP-ROLLOUTS.md)을 따라야 한다. 기존 서비스 UID 및 실제 Worker 경로를 준비하기 전에는 실환경 검증 완료로 표시하지 않는다. 새 GCP Redis 인프라/검증 도구도 병합했지만 공통 배포 UI에 Redis 발급이 연결된 것은 아니다.
