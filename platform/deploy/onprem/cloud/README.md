# AWS ↔ GCP 클라우드 간 전환 (러너)

온프렘 전환 러너(`live-db-cutover`, `--all-jobs`)가 원본·대상이 모두 클라우드인 작업을 받으면 `migration.cutover` 대신 `migration.cloud`로 실행해요. 앱은 양쪽 클러스터(EKS·GKE)에 이미 떠 있다고 봐요. 이 문서는 코드가 기대하는 설정과 적용 순서예요. 이 PR 은 구현만 하고 아래 적용은 하지 않았어요.

```
화면 전환 제어 ─▶ 플랫폼 ─▶ 러너(온프렘) ─┬─ migration.cloud ── Kafka Connect(온프렘) ─▶ RDS ↔ Cloud SQL (CDC)
                                         └─ crosscloud serve-adapter(127.0.0.1:18080) ─▶ 온프렘 접수 큐(durable-ingress)
사용자 요청 ─▶ 온프렘 접수 큐 ─▶ 원본 앱(aws: EKS sample-app) ─(전환)─▶ 대상 앱(gcp: GKE sample-app)
```

## 러너가 하는 일

1. 플랫폼에서 명령을 받을 때 원본·대상 환경 종류도 같이 받아요(`source_provider`·`target_provider`). 둘 다 온프렘이 아니면 클라우드 경로예요.
2. `MIGRATION_CLOUD_CONFIG`의 `job` 절만 그 작업(`job_id`, `source`=`data_source`, `target`=`data_target`)으로 바꾼 `jobs/<job_id>.cloud.yaml`을 만들어요.
3. 명령을 실행하는 동안 `crosscloud serve-adapter`를 띄워요. 어댑터는 `migration.cloud`의 앱 제어 API(일시정지·비우기·대상 변경·재개·상태 점검)를 온프렘 접수 큐 제어 API 로 바꿔 줘요. 명령이 끝나면 어댑터도 내려요.
4. 상태는 `/var/lib/worker/cloud/`에 따로 남겨요. 보고할 때 `migration.cloud`의 `STOPPING`·`FAILED`를 플랫폼 단계 `SYNC_CONFIRMED`·`ABORTED`로 바꿔요.
5. 승인 명령은 바로 실행하지 않아요. 먼저 `crosscloud ingress-status`(읽기 전용)로 접수 큐·대상 앱 상태를 확인하고, 통과하지 못하면 승인하지 않고 그 결과를 플랫폼에 실패로 올려요. `CROSSCLOUD_TARGET_ENVIRONMENT`를 주면 대상 앱 신원도 함께 대조해요.
6. 지원하는 명령은 시작(`run`)·승인(`run --approve`)·재시도(`run --retry`)예요. 중단·강제 재시도는 `migration.cloud`에 없어서 설정 오류(exit 2)로 돌려줘요.

## 적용 순서

### 1. 러너 이미지
`migration-runner` 이미지(`migration/runner.Dockerfile`, migration 도구 + crosscloud)를 씁니다. `migration-images` 워크플로가 GHCR 에 올려요. gitops `apps/live-db-cutover/runner.yaml`의 이미지를 이것으로 바꿔요.

### 2. 러너 설정과 비밀값 (gitops `apps/live-db-cutover`)
- ConfigMap `live-db-cutover-config`에 `config.cloud.yaml`을 더해요. 형식은 [config.cloud.example.yaml](config.cloud.example.yaml)이고, 원본·대상 이름은 어댑터 규칙에 따라 반드시 `aws`·`gcp`예요. 대상 DB 이름은 원본과 달라야 해요.
- `ingress.env`를 더해요. 형식은 [ingress.env.example](ingress.env.example)이고, `INGRESS_TARGETS`의 `aws`·`gcp`가 양쪽 `sample-app` 주소예요.
- 러너 env:
  - `MIGRATION_CLOUD_CONFIG=/etc/live-cutover/config.cloud.yaml`
  - `CROSSCLOUD_INGRESS_ENV=/etc/live-cutover/ingress.env`
  - `CROSSCLOUD_INGRESS_CONTROL_URL=<접수 큐 제어 API 주소>`
  - `INGRESS_CONTROL_TOKEN`(이미 있음)
- Secret 에 Cloud SQL 계정 비밀번호 키를 더하고 러너 env 로 넘겨요: `MIGRATION_GCP_ADMIN_PASSWORD`, `MIGRATION_GCP_APP_PASSWORD`, `MIGRATION_GCP_CDC_PASSWORD`, `MIGRATION_GCP_SINK_PASSWORD`, `MIGRATION_GCP_VERIFY_PASSWORD`. Cloud SQL 서버 CA 는 `cloudsql-ca.pem`으로 붙여요.
- Kafka Connect(`apps/migration-cdc`)에도 같은 Cloud SQL 비밀번호 env 를 넣어요. 커넥터는 `${env:...}`로 읽어요.

### 3. 접수 큐 (gitops `apps/durable-ingress`)
`INGRESS_TARGETS`에 `aws`(EKS `sample-app`, 지금의 `aws_eks`와 같은 주소)와 `gcp`(GKE `sample-app`)를 더하고, GKE 요청 토큰 Secret 을 `GKE_REQUEST_API_TOKEN`으로 넘겨요. 전환 대상 사용자 요청이 이 접수 큐를 거쳐야 전환 순간에 쓰기를 멈출 수 있어요.

### 4. 네트워크
- 온프렘 Kafka Connect·러너 → RDS: 지금처럼 Tailscale(AWS 라우터가 VPC 대역 광고)로 닿아요.
- 온프렘 Kafka Connect·러너 → Cloud SQL: 새 길이 필요해요. 둘 중 하나예요.
  - Cloud SQL 승인 네트워크에 온프렘 출구 IP 추가(공인 IP 사용, TLS `verify-full`)
  - GCP 쪽 Tailscale 라우터가 Cloud SQL 비공개 대역을 광고하고 온프렘 노드가 `--accept-routes`로 받기
  Terraform(oneaction-infra `infra/gcp`)에 반영해요.
- 접수 큐 → GKE `sample-app`: 공개 Ingress 주소 또는 Tailscale 경로.

### 5. DB 준비
`crosscloud prepare-target`(datapath)으로 이전용 계정과 빈 대상 스키마를 만들어요. 원본 RDS 는 `rds_replication`, 대상 Cloud SQL 은 `REPLICATION` 속성을 쓰고, 두 DB 모두 논리 복제가 켜져 있어요(`rds.logical_replication=1`, `cloudsql.logical_decoding=on`). 전환 전 `db_readiness`로 점검해요.

### 6. 승인 전에 사람이 할 일
crosscloud 런북(`crosscloud/deploy/rehearsal/README.md`)처럼, READY 뒤 승인 전에 대상 앱이 대상 DB 를 보도록 바꿔야 해요(`crosscloud prepare-target`: 대상 DB 로그인 생성, 대상 앱 재배포, `/readyz`·`/api/info` 확인). 러너는 이 단계를 자동으로 하지 않아요. 지금 `prepare-target`은 Cloud Run·ECS 재배포만 지원하고 GKE 재배포 경로는 없어서, GKE `sample-app`은 사람이 DB 접속 Secret 을 바꿔 재배포해야 해요. 이 단계가 끝나지 않으면 승인 전 `ingress-status`가 실패해서 승인이 막혀요.

### 7. 작업 만들기와 실행
```sh
POST /api/v1/migrations
{"source_env": "aws", "target_env": "gcp", "data_source": "aws", "data_target": "gcp"}
```
화면의 전환 제어로 시작 → 승인 대기(READY) → 승인. 반대 방향(GCP → AWS)은 `source_env`·`target_env`·`data_source`·`data_target`을 바꾼 새 작업이에요. 같은 작업으로 방향을 되돌리지 않아요.
