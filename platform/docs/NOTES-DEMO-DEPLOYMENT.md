# Notes 시연앱 이미지 배포 준비

대상 저장소: https://github.com/softbank-hackathon-2026-term1-azalea/oneport-platform-app-example

2026-10-03 FastAPI 앱을 빌드해 private ECR에 보관한 뒤, 같은 이미지를 공개 Docker Hub `inhoman/oneport-notes-demo`에 업로드했다. 아래 이미지는 **로컬 DB/Redis 연결과 Docker Hub 익명 pull까지 검증**했지만 **온프렘에는 아직 배포하지 않았다**. 기존 `onprem-smoke`는 DB 없는 별도 앱이다.

## 준비한 이미지

```text
docker.io/inhoman/oneport-notes-demo@sha256:01400bc9e6409dbeccd9865c22fe32fb8d06c350562549acc7a36ba3a2d7b1b7
```

- 앱 버전: 1.2.0, 플랫폼: linux/amd64
- 소스 커밋: `4ee5affb5909735a6f2765eda4a27c9d61d7f455`
- Dockerfile: `notes-fastapi/Dockerfile`, build context: `notes-fastapi`
- `USER app`을 `USER 999:999`로 바꿔 Kubernetes의 `runAsNonRoot`가 UID를 확인할 수 있게 했다. 파일 소유권과 실행 계정의 실제 UID는 동일하다.
- 수정 PR: https://github.com/softbank-hackathon-2026-term1-azalea/oneport-platform-app-example/pull/1
- 이미지 태그는 수정 커밋 SHA이며, 빌드할 때 `GIT_SHA`도 같은 값으로 전달했다. 배포에는 위 digest 주소를 사용한다.
- 공개 저장소: https://hub.docker.com/r/inhoman/oneport-notes-demo . 빈 Docker 인증 설정으로 digest pull 성공, Docker Hub API의 `is_private=false` 확인. 기존 ECR 이미지와 digest가 같다. 로컬에 레이어가 있어 익명 pull에서는 레이어를 다시 다운로드하지 않았다.

이 앱은 PostgreSQL이 필수다. Redis는 방문 수 카운터용 선택 기능이다. 레지스트리에 이미지를 올렸다는 사실만으로 DB 연결이나 온프렘 배포가 완료되지는 않는다.

## 검증 결과

격리된 로컬 Docker 환경에서 해당 Linux amd64 이미지와 PostgreSQL 17, Valkey 8을 실행했다.

| 항목 | 결과 |
|---|---|
| 숫자 UID | 이미지 `Config.User=999:999` 확인 |
| 초기 DB 마이그레이션 | 앱 시작 과정에서 완료 |
| `/ready` | HTTP 200, database=up, cache=up |
| `/version` | 1.2.0, 위 소스 커밋 일치 |
| 노트 생성·조회·완료 처리·삭제 | 통과 |
| Redis 방문 수 증가 | 통과 |
| `X-Launchpad-Release` 헤더 | 주입한 값 일치 |

검증용 컨테이너·네트워크·임시 데이터는 정리했다. ECR과 Docker Hub에는 앱 이미지가 남아 있으며 새 AWS 실행 서비스나 DB를 생성하지 않았다.

## 온프렘 연결 전 필요한 것

초기 확인에서는 StatefulSet/PVC 권한이 거부됐지만, 2026-10-03 후속 확인에서 기존 플랫폼 계정의 StatefulSet create/patch, PVC create, Job delete가 허용됐다. 전체 권한과 실제 리소스 생성은 추가 검증 대상이다. 기존 DB 연결 Secret은 아직 이 앱 환경에 등록하지 않았다.

빠른 첫 시연은 인프라에서 앱 전용 PostgreSQL을 미리 준비하고 플랫폼이 연결하는 방식으로 진행할 수 있다. 플랫폼 자동 생성으로 진행하려면 최소 권한 추가와 [데이터 서비스 생성 구현](ONPREM-DATA-SERVICES.md)이 둘 다 필요하다.

인프라와 맞출 계약:

1. 온프렘 앱 Pod에서 접근 가능한 PostgreSQL 내부 Service·앱 DB·앱 전용 사용자.
2. `DATABASE_URL`을 담은 앱 전용 immutable Kubernetes Secret. 플랫폼에는 Secret **이름·key·UID**만 등록한다. 비밀번호를 UI/문서/Git으로 전달하지 않는다.
3. 레지스트리 접근. 위 공개 Docker Hub 이미지는 pull용 계정이나 imagePullSecret 없이 접근 가능하다. 실제 클러스터의 네트워크 접근과 pull 성공은 배포 시 검증한다. 기존 smoke 환경의 private ECR 인증 설정은 변경하지 않았다.
4. 새 앱·환경 `onprem-notes-py / onprem-notes-demo`와 공개 hostname을 등록한다. 기존 `onprem-smoke` 설정에 새 앱 이미지만 바꾸어 배포하지 않는다.
5. API와 Worker의 새 환경 설정에 같은 이미지 allowlist, Secret binding, 플랫폼 DB를 연결하고 Worker를 시작한다. 이 이미지가 아직 현재 실행 중인 API의 등록 환경에 추가된 상태는 아니다.

운영자 환경의 연결 예시(값은 준비 후 채운다):

```json
{
  "secret_refs": {
    "notes-db": {
      "name": "<app-owned-secret-name>",
      "key": "DATABASE_URL",
      "uid": "<observed-secret-uid>"
    }
  }
}
```

## 플랫폼에서 입력할 값

[배포 요청 예제](../examples/onprem/notes-request.json)는 위 이미지와 DB binding을 지정한 요청이다. 환경·Secret 등록 전에는 실행할 수 없다.

| 입력 | 초기 시연 값 |
|---|---|
| 이미지 주소 | 위 공개 Docker Hub digest 주소 |
| 포트 | 8080 |
| health 경로 | `/ready` — DB 연결까지 확인 |
| replicas | 1 |
| DB | PostgreSQL, `DATABASE_URL` → `notes-db` |
| Redis | 처음에는 끄고, 캐시 준비 후 `REDIS_URL` binding과 함께 활성화 |
| migration | 별도 명령 생략: 현재 앱이 기동할 때 advisory lock을 사용해 실행 |

Redis를 끄면 노트 기능은 사용할 수 있지만 방문 수 기능은 사용할 수 없다. Redis를 붙이면 `/ready`가 cache=up인지도 확인한다.

이 앱의 `APP_FORCE_UNHEALTHY=true`는 **`/health`만 503으로 바꾼다**. 위 `/ready` 설정에서는 그 플래그만으로 실패 후보를 재현할 수 없다. 해당 실패 시연을 할 때는 health 경로를 `/health`로 정하고, DB/Redis 읽기·쓰기는 별도로 검증한다. `/health` 200 자체를 데이터 연결 성공으로 해석하지 않는다.

## 이미지 주소 입력과 Git 빌드의 차이

원래 ECR에 보관한 이미지는 private이며 위 Docker Hub 저장소는 public이다. private ECR 경로를 계속 사용한다면 별도 pull 인증이 필요하고, ECR 인증 토큰의 유효기간은 12시간이다. 공개 Docker Hub로의 복사는 기존 ECR 설정이나 인증을 변경하지 않았다. [AWS 인증 문서](https://docs.aws.amazon.com/AmazonECR/latest/userguide/registry_auth.html)

2026-10-03 사용자가 시연 이미지 공개를 허용했다. 로컬 Docker credential helper에서 `inhoman` 로그인 계정을 확인해 위 공개 저장소로 push했고, 익명 pull을 검증했다. 공통 입력은 `docker.io/<namespace>/<app>@sha256:<digest>`이며, 이미지에는 DB 비밀번호나 클라우드 자격증명을 포함하지 않는다.

Cloud Run은 공개 Docker Hub 이미지를 직접 사용할 수 있다. Google은 가용성을 위해 Artifact Registry 사용을 권장한다. 따라서 모든 외부 이미지에 복사가 필수인 것은 아니다. 필요하면 같은 빌드 결과를 Artifact Registry에 복사하거나 remote repository를 연결하고 원본·대상 digest를 기록한다. [Cloud Run 이미지 배포 문서](https://docs.cloud.google.com/run/docs/deploying)

공통 Worker는 공개 Docker Hub 입력을 실행 전에 검증한다. 익명 토큰으로 manifest와 config를 조회하고 digest 무결성·Linux CPU 아키텍처를 확인한 뒤 이력에 기록한다. private/없는 이미지·아키텍처 불일치는 Provider 리소스를 만들기 전에 실패하며, 429/일시 장애는 제한된 재시도로 처리한다. 사용자 입력으로 임의 레지스트리나 인증 서버에 요청하지 않는다.

AWS는 기존 앱 ECR 경로에 더해 운영자 `allowed_images`에 명시된 Docker Hub digest를 허용하며 해당 주소를 Fargate Task Definition에 전달한다. GCP는 `engine: cloud_run`인 native SDK Adapter가 동일한 주소를 Cloud Run revision에 전달한다. 기존 GitHub bridge는 Git 빌드 경로로 유지되며 이미지 입력과 혼동하지 않는다. 온프렘은 기존 Argo Worker가 같은 주소를 Pod에 전달한다. 공개 레지스트리를 쓴다고 기존 이미지 허용 정책을 제거하지 않는다. 코드 연결과 세 환경의 실제 배포 검증 결과는 구분한다.

플랫폼의 컨테이너 이미지 입력칸은 요청의 `source.image`를 채운다. 이미지 허용 정책이나 private registry 인증을 자동으로 생략하는 기능은 아니다.

Git 입력을 원터치로 지원하려면 공통 Build Worker가 커밋·Dockerfile·context를 받아 빌드/push하고, 결과 digest를 해당 앱의 허용 이미지로 등록한 뒤 기존 배포 요청에 연결해야 한다. 이번 준비는 운영자가 수행한 빌드이며 온프렘 웹의 Git 빌드 자동화가 완료된 것은 아니다.

## 플랫폼을 EC2에서 실행할 때

플랫폼의 실행 위치와 앱 이미지 저장소는 별개다. FastAPI·Worker가 EC2로 이동해도 배포 요청에는 같은 Docker Hub digest 주소를 전달할 수 있다. 위 이미지는 **배포 대상 Notes 앱 이미지**이며 Oneport UI/API/Worker를 실행하는 이미지가 아니다. 이번 변경은 EC2에 플랫폼을 설치하는 변경이 아니다. Provider 코드 연결과 실제 배포 검증 상태는 아래 결과를 기준으로 구분한다.

| 경로 | EC2 이전 시 준비 |
|---|---|
| 사용자 → 플랫폼 | 공개 HTTPS 443, 필요 시 HTTP 80 리다이렉트. React 정적 빌드와 `/api` reverse proxy, 실제 도메인의 Origin 설정 |
| API·Worker → 플랫폼 DB | 같은 플랫폼 PostgreSQL과 앱/환경 설정 사용. Mac의 루프백 DSN을 복사하지 않고 EC2에서 접근 가능한 내부 주소로 변경 |
| Worker → Provider | AWS/GCP SDK 인증과 HTTPS API 접근, 온프렘의 제한된 kubeconfig를 비밀 파일로 전달 |
| 온프렘 후보 검증 | EC2에서 내부 Service DNS에 접근할 수 없다면 기존 preview Job 모드 사용. Job 생성/조회 권한과 probe 이미지 접근 필요 |
| 실행 환경 → Docker Hub | 이미지 다운로드용 외부 HTTPS 및 DNS 접근. 공개 이미지 pull에는 사용자 계정 불필요. 실제 각 환경에서 pull 검증 필요 |
| 빌드 Worker → Docker Hub | EC2에서 빌드/push까지 할 경우 Docker/buildx와 해당 저장소의 push 인증 필요. Mac Docker Desktop 로그인은 EC2에 자동 전달되지 않음 |

FastAPI 8320과 플랫폼 DB 5432는 내부 연결용이며, Worker에는 별도 공개 수신 포트가 필요 없다. 로컬 UI 개발 포트 4320과 Mac DB 매핑 포트 55433을 그대로 인터넷에 공개하지 않는다. ECR 권한을 가진 EC2 IAM role이 Docker Hub push 권한을 부여하는 것은 아니다.

EC2 운영자가 먼저 확인할 공개 이미지 pull 명령:

```sh
docker pull --platform linux/amd64 \
  docker.io/inhoman/oneport-notes-demo@sha256:01400bc9e6409dbeccd9865c22fe32fb8d06c350562549acc7a36ba3a2d7b1b7
```

이 명령은 이미지 다운로드만 수행한다. Notes 앱을 실행하려면 별도 PostgreSQL과 `DATABASE_URL`이 필요하다. 각 Provider 등록 환경의 이미지 allowlist·앱/Secret 연결이 준비되어야 하며, 이 주소만으로 DB까지 자동 생성되지는 않는다.

## 각 Worker에 공통 이미지를 연결하는 방법

1. AWS runtime JSON의 `allowed_images`에 전체 Docker Hub digest 주소를 등록한다. 비어 있는 목록은 여전히 해당 앱의 ECR만 허용한다. 공개 주소 등록이 다른 앱의 ECR 접근을 허용하지 않는다.
2. 온프렘 runtime JSON의 `allowed_images`에 같은 주소를 등록한다. 이전 ECR 이미지로 롤백할 계획이면 기존 이미지와 pull Secret도 유지한다.
3. GCP runtime은 [Cloud Run SDK 설정](GCP-ROLLOUTS.md)의 `engine: cloud_run`을 사용한다. 기존 서비스 UID·주소·실행 계정·DB 연결을 확인하고 같은 주소를 `allowed_images`에 등록한다. `service_access: public`은 이미 공개된 서비스의 probe/UI에 사용하는 설정이며 서비스 IAM을 공개로 변경하는 옵션이 아니다.
4. API bundle과 각 Worker가 같은 runtime 파일·플랫폼 DB를 사용하도록 연결하고 재시작한다. GCP native Worker 명령은 `python -m controlplane.execution.gcp.operator worker --config <runtime.json>`이다. Git 전용 bridge Worker로 이미지 요청을 처리하지 않는다.
5. 화면에서 대상 환경을 선택하고 공통 이미지 주소·앱 포트·health 경로를 확인해 배포한다. AWS 앱은 최초 앱 등록도 필요하다. 성공 판단은 큐 접수가 아니라 릴리즈 성공과 실제 공개 응답이다.

클라우드와 DB 연결을 분리해 확인할 수 있도록 DB 없는 release-demo도 공개했다:

```text
docker.io/inhoman/oneport-release-demo@sha256:4aa7beeb9060fd1f8ed144b06304b30a152fc19d934f0211f0719e84608efe17
```

앱 포트는 8080, health 경로는 `/healthz`, 일반 환경변수 `VERSION`으로 응답 버전을 바꿀 수 있다. 이 앱 성공은 Notes 앱 DB 자동 생성 성공을 뜻하지 않는다.

## 공통 이미지 실환경 검증 — 2026-10-03

DB 없는 release-demo v2를 새로 빌드해 Docker Hub에 push하고, 플랫폼 화면의 배포 버튼으로 AWS와 온프렘에 같은 이미지를 재배포했다.

```text
docker.io/inhoman/oneport-release-demo@sha256:155a6e8597f135c2832499479711787e8aed53901170d457c4b839a57ad772a9
```

| 대상 | 결과 | 근거 |
|---|---|---|
| AWS Fargate | 성공 | Release `85bdc1c36c4685fe6fc70e92`, 공개 HTTPS 200과 요청한 릴리즈 헤더 일치 |
| 온프렘 Argo Rollouts | 성공 | Release `901a6675d5f9bcf6f13ebd21`, 공개 HTTPS 200과 요청한 릴리즈 헤더 일치 |
| GCP native Cloud Run | 성공 | Release `82bd6b1115f4bb47d99a0d39`, UI 접수부터 자동 전환 완료, 공개 `/health` 200과 요청 릴리즈 헤더 일치 |

온프렘 Kubernetes 이벤트에서도 위 Docker Hub 이미지의 다운로드 성공을 확인했다. 기존 이미지 캐시만으로 통과한 결과가 아니다. GCP는 키 교체 후 별도 검증 서비스에서 실행했다. `/healthz` 예약 경로 충돌과 포트 프로토콜 기본값(`http1`)에 의한 잘못된 변경 감지를 수정했다. 최초 수정 확인에는 명시적인 운영 조정이 있었고, 최종 위 릴리즈는 새 UI 요청으로 수동 개입 없이 성공했다.

AWS 검증 앱 `registry-smoke-1003`은 검증 후 service·listener rule·target group 2개·task definition·알람·runtime role 정리를 완료했다. 공용 ALB·VPC·클러스터는 삭제 대상이 아니다. GCP 임시 서비스 `oneport-registry-smoke-1003`도 삭제했으며 기존 `demo` 서비스는 유지했다. 온프렘 시연 앱은 `https://onprem-smoke-1003.yubin.website`에 유지한다. 위 검증에는 DB·Redis를 사용하지 않았고 Notes 앱의 데이터 서비스 자동 생성은 별도 남은 작업이다.

이미지를 여러 exporter로 동시에 push/load하는 빌드에서는 로컬 출력과 원격 manifest의 digest가 다를 수 있다. 배포 주소는 push 이후 레지스트리에서 조회한 digest로 고정하고 공통 사전 검증을 통과시킨다.
