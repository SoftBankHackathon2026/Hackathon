# AWS·GCP 실제 배포 검증 순서

## 최초 사전 점검 기록 — 2026-10-02

- Docker Engine 실행 중.
- 플랫폼은 preview 모드. ONEPORT_CONFIG/ONEPORT_RUNTIME_CONFIG 미설정.
- AWS `launchpad` 세션 만료. `default`는 root 인증이며 이 작업에서는 조회에만 사용함.
- 서울 리전에 팀 ECS cluster `oneport`, active ALB `oneport-alb` 확인.
- 기존 ECS service `notes-py`, `notes-java`, ECR `oneport/notes-py`, `oneport/notes-java` 확인.
- RDS `oneport-db-notes-py`, `oneport-db-notes-java`는 available. 앱별 인스턴스이며 Shared 시연 준비가 완료됐다는 증거가 아님.
- 로컬 GCP ADC 파일, GOOGLE_APPLICATION_CREDENTIALS, GCP_CONFIG_FILE 미확인/미설정. gcloud는 현재 PATH에서 찾지 못함. 현재 팀 GCP API는 gcloud보다 서비스 계정 ADC 주입이 필수임.
- 이번 사전 점검은 실제 새 배포 성공을 검증한 것이 아님. 유료 리소스 생성·기존 서비스 변경·삭제를 실행하지 않음.

## 추가 점검 — 2026-10-03

- 사용자가 저장소 루트에 제공한 `.env`로 `oneport-deployer` 역할 인증에 성공했다. Git 제외와 파일 권한 0600을 확인했다.
- ECS cluster ACTIVE, HTTPS 443 listener, 앱 로그 그룹, service deployment 조회 권한을 확인했다. 이 설정에서는 `launchpad` 재로그인이 필요하지 않다.
- 테스트 앱 이름은 `sdk-smoke-1003`으로 분리했다. 실제 ECR push와 플랫폼 API → AWS Worker → Fargate 최초 배포를 수행했다. 아래 결과를 참고한다.
- 제보된 DB 재설정/동시 실행/빌드 인증/DB 필수 검증 결함을 먼저 수정했다. [결함 분석과 검증 범위](AWS-PROVISIONING-SAFETY.md)를 참고한다.
- 이후 전달된 `gcp-config.json`, `onport-key.json`으로 GCP 인증과 실제 배포를 완료했다. 파일은 Git 제외 및 권한 0600으로 보관한다. 상세 결과와 기존 설정 복원은 아래 GCP 항목을 참고한다.

## 인프라 담당자에게 받을 것

비밀 키 원문을 채팅이나 Git에 넣지 않고 안전한 로컬 파일/Secret 경로로 전달받습니다.

| AWS | GCP |
|---|---|
| 실제 기반 환경 값이 채워진 `.env` (infra/aws/data-access.md 형식) | 실제 `gcp-config.json` |
| oneport-deployer를 사용할 수 있는 개발자 인증. 현재 root 인증을 배포 인증으로 사용하지 않음 | 현재 GCP API가 요구하는 서비스 계정 자격 증명 파일(저장소 외부) |
| 테스트용 앱 ID, 해당 앱 ECR digest, 포트, health 경로 | 현재 API가 갱신할 `demo` 서비스 사용 가능 여부와 실제 URL |
| DB 2차 시험을 위한 앱 전용 Shared DB Secret 참조 | 테스트할 공개 GitHub 주소(루트에 Dockerfile 필수) |

현재 `launchpad`는 만료된 Administrator 로그인입니다. 재로그인만으로 팀 배포 역할 사용이 보장되는 것은 아니므로 실제 배포 역할의 신뢰 정책과 개발자 인증을 함께 확인합니다. 기반 인프라를 새로 만드는 Terraform apply는 현재 준비 단계에 포함하지 않습니다.

## 1차 — DB 없는 앱으로 실제 배포

1. AWS는 기존 notes 서비스와 겹치지 않는 테스트용 app ID를 등록합니다. 허용된 앱 전용 ECR 경로에 Linux amd64 이미지를 준비합니다. `/healthz` 응답 및 `X-Launchpad-Release` 헤더를 제공해야 합니다.
2. GCP는 현재 API가 기존 `demo`만 갱신하므로 팀의 시험 대상으로 사용할 수 있는지 확인합니다. 새 서비스 생성으로 자동 대체하지 않습니다. 공개 GitHub 기본 HEAD의 루트 Dockerfile을 빌드하고 `/`·`/health`가 200이어야 합니다.
3. [통합 설정](CLOUD-PROVIDER-INTEGRATION.md)에 따라 공통 플랫폼 DB, AWS/GCP runtime, ONEPORT_CONFIG를 등록합니다. 모든 Worker가 같은 플랫폼 DB를 사용합니다.
4. API + UI + AWS Worker + GCP 별도 API(localhost:8740, 단일 worker) + GCP bridge Worker를 실행합니다. 운영 UI는 localhost:4320을 사용합니다. 4330의 현재 개발 프리뷰는 실행 환경 미등록 상태입니다.
5. **배포 · 릴리즈 → 운영자 접속 → 환경 선택 → 요청 확인 → 배포 시작**.
6. 반환된 Release ID, 이미지 digest, 실제 ECS task definition/Cloud Run revision, 단계별 이벤트를 기록합니다.
7. 성공 표시만 확인하지 않고 URL 응답을 확인합니다. GCP 인증 URL이면 인증된 검증 결과와 외부 공개 접속 결과를 구분합니다.

AWS 이미지 주소와 GCP GitHub 주소는 같은 `DeploymentSpec`의 source 변형입니다. 현재 지원 기능이 달라 동일 이미지 직접 입력을 GCP에서 시험할 수는 없습니다.

## 2차 — Shared DB 연결과 데이터 유지

각 환경의 Shared 인스턴스 안에 앱 전용 DB·계정을 준비한 뒤 `DATABASE_URL` Secret을 연결합니다. 앱에서 데이터를 생성·조회하고, v2 배포 후 같은 데이터가 남아 있는지 검사합니다.

현재 GCP 배포 API는 신규 Secret 연결 설정을 받지 않습니다. 사전에 연결된 서비스 설정으로 확인하거나, 담당 API에 Secret 전달 기능을 먼저 보완해야 합니다. PostgreSQL 인스턴스만 존재하는 것으로 통과 처리하지 않습니다.

팀 notes 예제는 서브폴더 `notes-fastapi/Dockerfile` 구조입니다. 현재 GCP API가 요구하는 루트 Dockerfile 구조와 바로 호환되지 않으므로, 테스트용 루트 레포를 준비하거나 빌드 context 입력 지원을 추가해야 합니다.

## 3차 — 재배포·실패·복구

- 정상 v1 → 정상 v2: 새 버전이 실제 응답하는지 확인.
- health 실패 버전: 성공 처리되지 않는지, 기존 정상 서비스가 어떻게 유지·복구되는지 확인.
- 같은 요청 키 재전송: 중복 Release가 생성되지 않는지 확인.
- GCP 응답 유실: `uncertain`으로 남겨 운영자가 확인하며 새 POST를 반복하지 않는지 확인.

공통 수동 rollback API는 아직 없습니다. AWS bake 기간 내 실패 복구와 온프렘 Worker 복구, GCP의 확인 필요 상태는 각 Provider 지원 범위대로 판정합니다.

## 기록과 정리

검증 기록에는 Provider, Release ID, digest, revision/task definition, URL, health 결과, DB 읽기·쓰기, 실패/복구 결과, 임시 자원 목록을 남깁니다. 비밀번호·토큰은 기록하지 않습니다.

테스트 종료 후 이번에 만든 임시 앱 자원만 정리합니다. 팀의 공용 VPC·ALB·ECS cluster·기존 notes 앱·공유 DB는 테스트 정리 대상으로 포함하지 않습니다. 기존 서비스의 이전 이미지와 설정을 먼저 기록해 복구할 수 있게 합니다. 신규 비용은 이전에 정한 3만 원 범위 안에서 확인하고 진행합니다.

## 실제 AWS 최초 배포 결과 — 2026-10-03 00:33 KST

| 항목 | 결과 |
|---|---|
| 앱 / 환경 | `sdk-smoke-1003` / `aws-live-smoke` |
| 입력 | `platform/examples/aws-smoke`를 Linux amd64로 빌드한 ECR digest |
| digest | `sha256:7421dbce6dad328261d7871d342a861fae05c819ba23b2b2e41b86bff0deba59` |
| 플랫폼 Release | `dfe583a9c5a4678971d7ce3e` |
| Task Definition | `oneport-sdk-smoke-1003:1` |
| 실행 사양 | Fargate 0.5 vCPU / 1024 MiB / 1 replica |
| 접속 | `https://sdk-smoke-1003.app.yubin.dev/healthz` |
| 결과 | HTTPS 200, healthy=true, 응답의 릴리즈 ID가 플랫폼 Release와 일치 |
| 플랫폼 상태 | `succeeded`, UI에서 배포 완료 및 실제 서비스 링크 확인 |

사용한 경로는 localhost:4320(Vite proxy) → FastAPI:8320 → PostgreSQL → 별도 AWS worker → boto3 → ECS다. 최초 등록은 UI로, 첫 요청은 동일한 HTTP API로 접수했다. 재배포는 UI 버튼에서 검증한다. DB/Redis/마이그레이션은 이 시험에 포함하지 않는다. 검증 이후 임시 실행 리소스를 정리하면 위 서비스 URL은 더 이상 응답하지 않는다.

현재 로컬 실행 설정은 Git에서 제외된 `platform/.data/aws-live/platform.json`, `runtime.json`, `operator-token`에 있다. 다른 개발자는 자신의 환경 값으로 [통합 설정](CLOUD-PROVIDER-INTEGRATION.md)을 작성해야 한다. 화면은 `http://127.0.0.1:4320/#releases`이며 4330은 별도의 개발 프리뷰다.

재시작 순서(로컬 운영자 터미널에서):

```bash
export ONEPORT_CONFIG="$PWD/platform/.data/aws-live/platform.json"
export ONEPORT_OPERATOR_TOKEN="$(cat platform/.data/aws-live/operator-token)"
uv run --project platform/backend --extra aws uvicorn controlplane.main:app --host 127.0.0.1 --port 8320
# 다른 터미널
uv run --project platform/backend --extra aws python -m oneport_aws.execution.oneport_ecs_operator worker --config "$PWD/platform/.data/aws-live/runtime.json"
# 다른 터미널
npm --prefix platform/frontend run dev
```

앱별 ALB priority는 웹 등록 시 임시 배정하고, worker가 실제 listener의 기존 rule과 비교해 충돌 없이 재할당한다. 다른 팀 앱의 rule은 수정하지 않는다. 배포 전 이미지 digest는 앱 전용 ECR repository와 허용 이미지 목록에 맞아야 한다.

## 실제 UI 블루그린 재배포 결과 — 2026-10-03 00:36 KST

- 웹 UI에서 `env.VERSION`을 `v2`로 바꾸고 **배포 시작** 버튼을 눌렀다. 동일 이미지 digest의 설정 변경 배포이며, 별도의 v2 이미지를 빌드한 시험은 아니다.
- Release `8da1b63bcde94067453b818f`, Task Definition `oneport-sdk-smoke-1003:2`.
- bake 중 구·신 Task 2개가 실행되었고, ALB 대상 그룹 가중치가 green 100%에서 blue 100%로 바뀐 것을 SDK 조회로 확인했다. 색상 이름은 재배포마다 역할이 바뀐다.
- `/healthz` HTTPS 200, `version=v2`, `X-Launchpad-Release=8da1b63bcde94067453b818f`를 확인했다. 플랫폼 상태와 UI는 `succeeded` / 배포 완료다.
- 첫 배포와 재배포를 합쳐 두 번 성공했다. 실패 버전의 실제 롤백 시험, DB 읽기·쓰기, GCP 실환경 배포는 이 결과에 포함하지 않는다.

## AWS 시험 종료와 비용 정리

2026-10-03 테스트용 ECS service, ALB host rule, blue/green target group, 활성 Task Definition 등록, 배포 알람, 앱 runtime role을 정리했다. 해당 앱의 실행 중 Task가 0개이고 host rule이 없음을 SDK로 확인했다. 기존 `notes-py`/`notes-java`의 task definition과 desiredCount는 시험 전과 동일하다.

재배포용 ECR 이미지, CloudWatch 시험 로그, 플랫폼 배포 이력은 유지했다. 이 저장 리소스의 소액 저장 비용 및 팀 공용 인프라의 기존 비용까지 0이라고 주장하지 않는다. 테스트 URL은 정리 후 서비스 접속에 사용할 수 없다. 로컬 플랫폼과 worker는 다시 배포할 수 있도록 실행 중이며, 새 배포를 누르면 유료 Fargate 실행 자원이 다시 생성된다.

## 실제 GCP UI 배포 결과 — 2026-10-03 00:50 KST

| 항목 | 결과 |
|---|---|
| 입력 | `https://github.com/hyukvoid/cloud-bridge-e2e-sample.git` |
| Git commit | `63b0f0633346f9bb09fc279d16dcf6f325bbf09a` |
| 플랫폼 Release | `cdcd45e942cfb96787fbaa5e` |
| GCP 작업 | `7258801297aa47359cebc8a15a80e19c` |
| Cloud Run revision | `demo-cb-fbe42519` |
| digest | `sha256:e3e4438197bfbf4589f7fd145800594a04c812accda245451bf218b751c2fe8c` |
| URL | `https://demo-q37hkgfc2a-du.a.run.app` |
| 배포 결과 | UI 버튼 → 공통 API → PostgreSQL → GCP bridge → 팀 GCP API → Cloud Run, 플랫폼 `succeeded` |
| 독립 확인 | 새 revision에 트래픽 100%, ID token 인증 요청 `/` 및 `/health` 모두 200 |
| 공개 접속 | 비인증 `/health` 요청은 403. 일반 사용자 공개 서비스로 검증된 것은 아님 |

첫 시도는 같은 Git commit 태그를 다시 push하려다 immutable Artifact Registry에서 실패했다. [GCP PR #1](https://github.com/softbank-hackathon-2026-term1-azalea/gcp-deployment-lab/pull/1)의 수정으로 기존 태그의 검증된 digest를 재사용하고 새 Cloud Run revision을 만들었다. 로컬 빌드는 수행했지만 성공한 재시도에서 이미지를 새로 push하지는 않았다.

검증한 팀 API 소스는 `feature/gcp-bluegreen-rollback`의 `b2e7e40`에 수정 `3c1176f`를 적용한 상태다. 이번 공통 bridge 요청은 기본 **direct** 배포다. 팀 API의 blue/green 기능이 존재하더라도 공통 UI에서 blue/green·rollback을 검증했다는 의미는 아니다. Shared DB, Redis, Migration도 이번 시험에서 제외했다.

시험 후 기존 컨테이너 이미지와 원래 revision별 트래픽·tag 설정을 복원했다. 네트워크·서비스 계정·스케일 설정이 기존 값과 같은지 확인했으며, min instances는 0이고 새 Cloud Run 서비스는 생성하지 않았다. 기존 `demo` 서비스와 시험 revision 기록은 유지했다. 이후 URL 응답은 복원된 기존 버전의 응답이다.

GCP 별도 API 테스트는 **143 tests / 29 subtests passed**, 변경 파일 Ruff/format 통과다. 공통 플랫폼 138개 테스트와 별도로 실행한 결과이며, GCP PR #1은 아직 병합되지 않았다.

온프렘은 [접속 계약과 Worker 네트워크 준비](ONPREM-CONNECTION-HANDOFF.md)에 정리했다. kubeconfig 파일 경로가 확인되지 않아 실제 배포 성공 범위에 포함하지 않는다.

## 최신 main 통합 검증 — 실제 배포 이후

main의 온프렘 API 터널 변경(PR #17), GCP native rollout·Redis 인프라(PR #19)를 병합했다. GCP bridge를 별도 모듈로 보존하고 native adapter를 공통 SDK 및 승격·복귀 API에 연결했다. 실제 PostgreSQL을 사용하는 백엔드 **170 tests passed**, Ruff/format, React TypeScript/Vite build, wheel/sdist build를 통과했다. Native GCP Cloud Run 전송은 테스트 대역이며 이 병합 후 새 유료 배포를 실행하지는 않았다. 실제 GCP 성공 기록은 위 bridge direct 경로를 기준으로 한다.
