# 실클라우드 왕복 리허설 런북 (AWS→GCP→AWS)

sample-app 하나를 AWS 에서 GCP 로 옮기고(순방향), 다시 새 AWS DB 로
되돌린다(귀환). 자원을 만들고 지우는 일은 `python -m crosscloud.rehearsal`
이 단계별로 하고, 전환(원가연 `migration.cloud` 실행·승인)은 그 도구가
찍어 주는 명령을 사람이 한 줄씩 돌린다.

*   **상태 디렉터리(state-dir):** 단계마다 산출물(Terraform 변수·state·출력,
    인계 파일, 완료 표시)을 남긴다. 멈춘 곳에서 같은 명령을 다시 돌리면
    끝난 단계는 건너뛴다(다시 하려면 `--force`). 저장소 밖 경로를 쓴다.
*   **비밀값:** 파일·로그·명령줄에 남기지 않는다. 비밀번호는 Secret 이
    정본이고, 셸 env 는 `eval "$(... env <job>)"` 로 Secret 에서 읽어 채운다.
*   **범위(2026-11-07 방향 변경):** 데모 원본은 팀 AWS 계정의 EKS
    oneaction sample-app + RDS, 대상은 팀이 띄울 GKE 동일 구성으로 바뀌었다.
    이 런북의 고정물 단계(ECS·Cloud Run 을 만드는 `aws-source`·`gcp-target`
    ·`prepare-forward` 의 앱 부분)는 자체 리허설용으로만 남기고 더 키우지
    않는다. 범용 부분(Terraform 출력 → render-handoff 변환, Secret·DB 주체,
    compose·접수 큐·어댑터 명령, 정리·잔여)은 그대로 쓴다. GKE 재배포 경로
    (`prepare-target` 의 k8s Secret 교체)는 아직 없다.
*   **dry-run:** 자동 단계는 `--dry-run` 으로 명령·Terraform 변수만 찍는다.
    클라우드·DB·상태 파일을 건드리지 않는다.

## 준비

```shell
cd crosscloud && . .venv/bin/activate     # pip install -e '.[dev]'
export STATE=$HOME/ccx-rehearsal/r1107a   # 저장소 밖
python -m crosscloud.rehearsal --state-dir "$STATE" init --run r1107a \
    --source-image docker.io/kty1021/sample-app@sha256:<index digest>
```

`--run` 은 영문 소문자·숫자 1–15자다. 자원 접두사는 `ccx-<run>`(첫 AWS
스택·GCP)과 `ccx-<run>c`(귀환 대상 새 RDS)다. Cloud SQL 이름은 지운 뒤
한동안 다시 못 쓰므로 리허설마다 새 run 을 쓴다.

## 순서

각 명령 앞에 `python -m crosscloud.rehearsal --state-dir "$STATE"` 를
붙인다. 소요는 어림이다(실측 전).

| 단계 | 명령 | 하는 일 | 소요 |
|---|---|---|---|
| 0 | `preflight` | 도구, AWS 계정(187478112557), GCP 프로젝트(ghost-project-513812), ADC, buildx 확인 | 1분 |
| 1 | `aws-source` | AWS 기반(VPC·RDS PG16 논리 복제·ECR·ECS 클러스터·Secret 그릇) → 팀 이미지 digest 를 ECR 로 복사 → RDS CA 묶음 → 원본 DB `shop` 씨앗(표·`shop_app`·시험 행 20·`ccx-<run>-database-url`) → ECS+ALB | 15–20분 |
| 2 | `gcp-target` | Artifact Registry → 이미지 복사 → Cloud SQL(`GOOGLE_MANAGED_CAS_CA`·`ENCRYPTED_ONLY`, DB `shop_b`)·Secret 그릇 → CA 합본 `db-ca.pem` → postgres 비밀번호(Secret 정본) | 15–20분 |
| 3 | `prepare-forward` | Secret 이관 `database-url` AWS→GCP(SHA-256 대조) → 양쪽 원가연 로그인·대상 스키마·프로필 B → Cloud Run(ingress all, 원본 프로필 A) → readiness 네 항목 | 5–10분 |
| 4 | `handoff-forward` | `crosscloud discover` → Terraform 출력에서 인계 입력·`target-deploy.yaml` → `render-handoff` | 1분 |
| 5·6 | `cutover-forward` | 전환 명령을 찍는다. 사람이 돌린다(아래) | 20–30분 |
| 7a | `prepare-return` | 귀환 대상 새 RDS(`ccx-<run>c`, DB `shop_c`) → 원본 Cloud SQL·대상 RDS 로그인·스키마·프로필 C → readiness | 10–15분 |
| 7b | `handoff-return` | 귀환 인계(대상 앱은 첫 ECS 서비스, 대상 프로필은 `ccx-<run>-database-url-c`) | 1분 |
| 7c | `cutover-return` | 귀환 전환 명령을 찍는다 | 20–30분 |
| 8 | `audit` | 감사 보존 버킷 2개를 만들고 이관 명령을 찍는다 | 5분 + 이관 |
| 9 | `cleanup` | 봇 중지 → 미처리 0 → compose down → destroy 역순 → 잔여 질의 | 15–25분 |

진행 현황은 `status` 로 본다. 합계는 어림 2–3시간이다.

### 전환 (5·6, 7c)

`cutover-forward` 가 찍는 명령을 **한 셸**에서 위에서부터 돌린다. 요약:

1.  `SESSION`·`SAMPLE_APP_IMAGE`·`INGRESS_ENV_FILE`·`INGRESS_CONTROL_TOKEN`
    (무작위)을 export 하고, `eval "$(… env forward)"` 로 비밀번호 env 를
    채운다.
1.  접수 큐·Redis, Kafka·Connect compose 를 띄운다.
1.  `crosscloud ingress-start … --allow-no-state` 로 접수 큐를 원본으로 연다.
1.  `crosscloud serve-adapter … &` 로 어댑터를 띄운다.
1.  `--profile bot up -d traffic-bot` 으로 봇을 띄운다.
1.  `python -m migration.cloud … run` — READY 에서 종료 코드 3.
1.  `crosscloud prepare-target …` — B LOGIN, Cloud Run 을 프로필 B 로 재배포,
    `/readyz`·`/api/info` 가 `shop_b` 로 연속 3회.
1.  `crosscloud ingress-status …` — 종료 코드 0 이 아니면 승인하지 않는다.
1.  `… run --approve ccx-<run>-forward` → `… status` 로 COMPLETED 확인.

귀환(`cutover-return`)은 순방향 어댑터를 멈추고(`kill %1`), `env return`
을 채우고, Connect 를 `connect.return.compose.yaml` 을 겹쳐 다시 띄운 뒤 같은
순서를 `return` 파일로 돈다. 접수 큐는 순방향 그대로 쓴다.

## 과금 자원

리허설 동안만 있다. 모두 `cleanup` 이 지운다.

| 클라우드 | 자원 | 사양 |
|---|---|---|
| AWS | RDS PostgreSQL 16 × 2 | db.t4g.micro, 20 GB, 단일 AZ, 공개 |
| AWS | ALB × 1 | HTTP 80, 인터넷 연결형 |
| AWS | ECS Fargate 작업 × 1 | 0.25 vCPU, 512 MB, 공인 IP |
| AWS | 공인 IPv4 | ALB·작업·RDS |
| AWS | Secrets Manager | 9개(접두사 `ccx-<run>`·`ccx-<run>c`) |
| AWS | ECR·CloudWatch Logs·S3(감사) | 저장량 소량 |
| GCP | Cloud SQL PostgreSQL 16 | db-f1-micro, ENTERPRISE, 10 GB, 단일 존 |
| GCP | Cloud Run 서비스 × 1 | 요청 기반 |
| GCP | 외부 Application LB·전역 주소 | HTTP 80(target_app 모듈) |
| GCP | Secret Manager·Artifact Registry·GCS(감사) | 소량 |
| 공통 | 클라우드 간 송신 | CDC·교차 DB 접속(시험 데이터, 소량) |

NAT 게이트웨이·VPN·Cloud NAT 는 없다.

## 중단했을 때 정리

어느 단계에서 멈췄든 먼저 `cleanup` 을 돌린다. state 가 있는 스택만 지우고,
compose 를 띄운 적이 없으면 그 부분을 건너뛴다. 미처리가 0 이 아니면 아무
것도 지우지 않고 멈춘다 — 접수 기록이 요청의 유일한 사본이다.

```shell
python -m crosscloud.rehearsal --state-dir "$STATE" cleanup
```

`cleanup` 이 실패하면 아래를 손으로 확인한다.

```shell
kill %1                                    # serve-adapter(같은 셸)
docker ps -a --filter label=com.ghost.crosscloud.session=<run>
docker volume ls --filter label=com.ghost.crosscloud.session=<run>
docker network ls --filter label=com.ghost.crosscloud.session=<run>
aws resourcegroupstaggingapi get-resources --region ap-northeast-2 \
    --tag-filters Key=crosscloud-run,Values=ccx-<run>,ccx-<run>c
gcloud sql instances list --project ghost-project-513812 --filter name~ccx-<run>
docker logout <ECR 호스트>; docker logout https://asia-northeast3-docker.pkg.dev
```

AWS Secret 은 `recovery_window_in_days = 0` 이라 destroy 때 바로 지워진다.
state 파일은 `$STATE/tf/*.tfstate` 에 있다. 손으로 지울 때도 그 state 로
`terraform destroy` 를 먼저 한다.

## 실행 전 확인

코드에서 확인하지 못한 사실이다. 리허설 첫 실행에서 하나씩 본다.

1.  **팀 CI 이미지 digest:** sample-app main `363eeb9`(전체
    `363eeb9298c1cee74d0cbc9dd5d4fb4b172b0c2e`) 의 CI 게시 태그는
    `docker.io/kty1021/sample-app:<전체 SHA>` 다(sample-app
    `.github/workflows/ci.yml`). index digest 는
    `docker buildx imagetools inspect` 로 읽어 `--source-image` 에 넣는다.
    게시됐는지, linux/amd64 가 있는지(Fargate·Cloud Run)는 미확인이다.
1.  **이미지 복사:** `docker buildx imagetools create` 의 단일 원본 복사가
    ECR·Artifact Registry 사이에서 digest 를 보존하는지. 다르면 `inspect`
    단계에서 멈춘다.
1.  **Cloud SQL 서버 CA:** `GOOGLE_MANAGED_CAS_CA` 에서
    `gcloud sql ssl server-ca-certs list --format=value(cert)` 가 CA 를
    주는지, 서버 인증서 SAN 에 공개 IP 가 있는지(설계 X-T3p). 앱 URL
    호스트는 공개 IP 다(`wiring._cloud_sql_endpoint`). SAN 에 IP 가 없으면
    `prepare-target` 의 신원 확인이 실패한다 — 그때는 인스턴스 DNS 이름
    으로 바꾼다.
1.  **CA 합본 크기:** `DB_SSL_CA` 가 ECS·Cloud Run env 한도 안인지.
1.  **원가연 실행 환경:** `python -m migration.cloud` 를 돌릴 파이썬에
    `migration` 의존성이 있어야 한다(`migration/pyproject.toml`).
1.  **봇:** 이미지 `node` 사용자가 `bot-ledger` 볼륨(`/home/node`)에 쓸 수
    있는지, alpine 에서 봇의 flock 이 도는지.
1.  **접수 큐 제어 바인드:** 이미지가 `INGRESS_CONTROL_HOST`·
    `INGRESS_CONTROL_TOKEN` 을 받는지(4ecee72 이후 빌드여야 한다).
1.  **GCP 권한·정책:** API 6종 켜짐(`terraform/google/README.md`),
    `target_app` 의 IAM 정책 설정 권한, 조직 정책
    `sql.restrictAuthorizedNetworks` 가 `0.0.0.0/0` 을 막지 않는지.
1.  **감사 원본:** CloudTrail 추적 버킷·접두사와 `gcloud logging copy` 대상
    버킷. 찍힌 명령의 `<…>` 를 채운다. S3 Object Lock 버킷에 보존 중인
    객체가 있으면 destroy 가 실패할 수 있다(거버넌스 우회 삭제 필요).
1.  **정리의 compose 자리값:** `cleanup` 은 compose 파일의 필수 비밀 env 에
    자리값을 넣고 `stop`·`down` 만 한다. 실제 값은 쓰지 않는다.
1.  **docker 자격 증명:** 레지스트리 로그인은 `~/.docker/config.json`(또는
    키체인)에 남는다. `cleanup` 이 `docker logout` 한다.

## 설계와 다른 점

*   귀환 전에 ECS 를 Cloud SQL 원본 프로필로 다시 띄우지 않는다(설계 3절 ⑥
    기본안). 그 사이 ECS 는 옛 원본 `shop`(원가연 차단 트리거) 을 읽기만
    하고, `prepare-target` 이 곧바로 프로필 C(`shop_c`)로 재배포한다.
*   귀환 대상 새 RDS 는 같은 AWS 모듈을 접두사 `ccx-<run>c`·`include_app =
    false` 로 한 벌 더 깐다. Terraform 모듈은 바꾸지 않았다.

## See also

*   [운영자 PC 전환 연결](../operator-pc/README.md)
*   [AWS 모듈](../../terraform/aws/README.md) ·
    [GCP 모듈](../../terraform/google/README.md)
*   원가연 [CLOUD_MIGRATION.md](../../../migration/CLOUD_MIGRATION.md)
