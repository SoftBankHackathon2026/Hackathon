# 운영자 PC 전환 연결 예시

클라우드 간 전환을 운영자 PC 에서 돌릴 때 띄우는 것과 그 순서다. 원가연
`migration.cloud` 가 전환 순서를 정하고, crosscloud 는 인계 파일·예열·확인
명령과 앱 제어 어댑터를 맡는다. 요청은 접수 큐(durable-ingress)가 받으므로
전환 중에도 접수는 거절되지 않고, 앱으로 전달만 멈춘다.

## 구성

| 무엇 | 어디 | compose 프로젝트 |
|---|---|---|
| 접수 큐 durable-ingress + Redis | `ingress.compose.yaml` | `ccx-<세션>-ingress` |
| Kafka + Connect(Debezium) | `connect.compose.yaml` | `ccx-<세션>-connect` |
| 앱 제어 어댑터 | `crosscloud serve-adapter`(호스트 프로세스) | — |
| 전환 상태 머신 | `python -m migration.cloud`(호스트 프로세스) | — |

두 compose 프로젝트를 나눈 이유: 승인 전에 취소하면 Connect 만 내려도 접수
기록(Redis AOF)이 남는다. Kafka 데이터도 이름 붙은 볼륨이라 Connect 를 내렸다
올려도 job 이 이어진다. 모든 포트는 호스트 `127.0.0.1` 에만 공개한다. 컨테이너·
망·볼륨에는 라벨 `com.ghost.crosscloud.session=<세션>` 이 붙는다.

## 순서

셸에 `SESSION`(세션 이름), `SAMPLE_APP_IMAGE`, `INGRESS_CONTROL_TOKEN` 과
render-handoff 가 알려 주는 비밀번호 env 를 Secret 에서 채운다. 값은 파일에
쓰지 않는다.

1.  인계 파일을 만든다. readiness 네 항목(`replication_flags` ·
    `migration_logins` · `target_schema_empty` · `profile_secrets`)이 모두
    준비돼야 쓴다. DB 주소는 `terraform output -json` 에서, 앱 DB 사용자는
    인자로 받는다. 대상 DB 이름은 원본과 달라야 한다. 앱 주소는 AWS 는 ALB
    주소, GCP 는 Cloud Run 서비스 URL(`terraform output service_uri`)이다.

    ```shell
    crosscloud render-handoff --inventory discover.json \
        --source-tf src-tf.json --target-tf tgt-tf.json \
        --target-database shop_b --source-app-user shop_app \
        --table public.migration_demo=request_id \
        --readiness readiness.json \
        --control-url http://127.0.0.1:18080 \
        --connect-url http://127.0.0.1:8083 \
        --source-app-url http://<원본 앱 ALB> \
        --target-app-url https://<대상 Cloud Run 서비스 URL> \
        --redis-url redis://redis:6379 --out-dir out
    ```

1.  접수 큐와 Connect 를 띄운다.

    ```shell
    docker compose -f ingress.compose.yaml up -d
    docker compose -f connect.compose.yaml up -d --build
    ```

1.  접수 큐를 원본 라벨로 돌리고 전달을 연다. durable-ingress 는 처음에
    `onprem` 대상·paused 로 뜬다(`ingress.env` 의 `onprem` 은 원본 앱 별칭).
    셸에 `INGRESS_CONTROL_TOKEN` 이 있어야 한다. 원가연 `cloud.lock` 을 쥔 채
    `--state-dir` 의 단계를 읽어 승인 전일 때만 한다. 원가연을 아직 한 번도
    돌리지 않아 상태 파일이 없으면 `--allow-no-state` 를 준다.

    ```shell
    mkdir -p out/state
    crosscloud ingress-start --cloud-config out/config.cloud.yaml \
        --ingress-env out/ingress.env \
        --ingress-control-url http://127.0.0.1:9092 \
        --state-dir out/state --allow-no-state
    ```

1.  어댑터를 연다(같은 셸에 `INGRESS_CONTROL_TOKEN`). 전환이 끝날 때까지 둔다.

    ```shell
    crosscloud serve-adapter --cloud-config out/config.cloud.yaml \
        --ingress-env out/ingress.env \
        --ingress-control-url http://127.0.0.1:9092
    ```

1.  원가연 복사를 시작한다. READY 에서 종료 코드 3 으로 멈춘다.

    ```shell
    python -m migration.cloud --config out/config.cloud.yaml \
        --state-dir out/state run
    ```

1.  READY 에서 대상 앱을 예열한다. 대상 app 로그인에 LOGIN 을 켜고(셸에 대상
    admin 비밀번호 env), 대상 앱을 B 프로필(접속 URL Secret·DB TLS env)로
    다시 배포한 뒤 `/readyz`·`/api/info` 가 대상 DB 이름으로 연속 3 회
    답하는지 본다. `target-deploy.yaml` 의 형식은 아래 절에 있다.

    LOGIN 을 켜기 전에 배포 명세가 이 job 의 대상인지 본다. `platform` 이
    대상 라벨과 맞아야 하고(`cloud_run`⇔`gcp`, `ecs`⇔`aws`), Cloud Run 은
    서비스 URL 이 `--target-app-url` 과 같아야 한다. 귀환 때 순방향 명세를
    넘기면 여기서 거부된다. Cloud Run 은 새 리비전에 트래픽 100% 를 주고,
    ECS 는 안정 뒤 PRIMARY 배포가 새 작업 정의인지 본다(롤백이면 실패).
    LOGIN 을 켠 뒤 실패하면 출력 JSON 의 `app_login` 이 그 사실을 알린다.

    ```shell
    crosscloud prepare-target --cloud-config out/config.cloud.yaml \
        --ingress-env out/ingress.env --state-dir out/state \
        --deploy target-deploy.yaml --db-ca-file target-db-ca.pem
    ```

1.  승인 직전 확인을 한다(읽기 전용). READY · 접수 큐 running·원본 대상·오류
    없음 · 대상 앱 신원이 모두 맞아야 종료 코드 0 이다.

    ```shell
    crosscloud ingress-status --cloud-config out/config.cloud.yaml \
        --ingress-env out/ingress.env \
        --ingress-control-url http://127.0.0.1:9092 --state-dir out/state
    ```

1.  승인한다.

    ```shell
    python -m migration.cloud --config out/config.cloud.yaml \
        --state-dir out/state run --approve <승인 id>
    ```

## target-deploy.yaml

대상 앱 배포를 B 프로필로 다시 배포할 때 바꿀 Secret 참조다(값 없음).

```yaml
platform: cloud_run            # 또는 ecs
service: projects/<p>/locations/<r>/services/<서비스>   # ecs 는 서비스 이름
# cluster: <ECS 클러스터 ARN>   # ecs 만
# region: ap-northeast-2        # ecs 만
secret_env:
  DATABASE_URL: {secret: <대상 프로필 Secret ID>, version: "1"}   # ecs: {secret: <ARN>}
```

## 어댑터가 하는 일

| 원가연 호출 | 어댑터 동작 |
|---|---|
| `GET /status` | 접수 큐 `mode` 를 `RUNNING`·`PAUSED` 로, 대상 이름을 라벨로 |
| `POST /pause` | 접수 큐 `/pause`, `settled:true` 확인 |
| `POST /drain` | 접수 큐가 paused·`in_flight` 0 이 될 때까지 대기 |
| `POST /target` | paused·`in_flight` 0 확인 → 대상 앱 DB 신원 확인 → `/target` |
| `POST /resume` | 큐가 대상 라벨일 때만, 최신 epoch 로 `/resume` |
| `GET /healthz` | 큐가 대상 라벨로 전달 중·오류 없음 + 대상 앱 DB 신원을 연속 3 회, 응답 직전 큐 재확인(25초 안) |

## 정리

실험이 끝나면 만든 것만 지운다. 접수 큐의 Redis 기록은 202 로 받은 요청의
유일한 사본이라, 미처리가 0 일 때만 지운다.

1.  트래픽 봇을 멈춘다(새 접수 0).
1.  미처리 0 을 확인한다. Redis `XINFO GROUPS ingress:inbox` 의 `delivery`
    그룹 pending=0·lag=0, 접수 큐 `in_flight`=0·`last_error` 없음이어야
    종료 코드 0 이다. Redis 는 DB 0·무인증만 받는다. DB 번호(`/1`)나
    비밀번호가 든 URL 은 오판을 막으려고 거부한다.

    ```shell
    crosscloud ingress-drain-check \
        --ingress-control-url http://127.0.0.1:9092 \
        --redis-url redis://127.0.0.1:6379
    ```

1.  0 이 아니면 지우지 않고 보고한다.
1.  0 이면 지운다. Connect 는 마지막 job 이 끝났거나 정리된 뒤에 지운다.

    ```shell
    docker compose -f ingress.compose.yaml down -v
    docker compose -f connect.compose.yaml down -v
    docker ps -a --filter label=com.ghost.crosscloud.session=$SESSION
    docker volume ls --filter label=com.ghost.crosscloud.session=$SESSION
    docker network ls --filter label=com.ghost.crosscloud.session=$SESSION
    ```

## 한계

*   대상 DB 이름은 원본과 달라야 한다(코드가 거부한다). 귀환(GCP→AWS)의
    대상 이름은 앞 job 의 원본(옛 A, 예: `shop`)과도 달라야 한다(예:
    `shop_c`). 설정 한 장에는 옛 A 이름이 없어 코드가 이것을 모른다.
*   `SAMPLE_APP_IMAGE` 는 팀 sample-app 이미지를 그대로 쓰되, 제어 바인드
    주소·토큰을 env 로 받는 커밋(4ecee72) 이후 빌드여야 한다. 그 전 이미지는
    제어 API 를 컨테이너 안 loopback 에만 열어 호스트에서 닿지 않는다.
*   대상 앱 요청 API 는 토큰 없이 연다(`REQUEST_API_TOKEN` 미설정 시 open).
    durable-ingress 는 토큰을 쓰는 대상에 https 만 허용하므로, http LB 대상은
    `token_env` 를 두지 않는다.
*   compose 예시는 `docker compose config` 로 형식을, 시험으로 Kafka
    `CLUSTER_ID`(16바이트 base64url 22자)를 확인했다. 실제로 띄워 보지는
    않았다.
*   GCP 앱 배포(`terraform/google/target_app`)의 Cloud Run 진입은 `all`
    이다. 운영자 PC 의 접수 큐가 서비스 URL 로 직접 보내기 때문이다.
*   ECS 는 배포 서비스 주소를 `--target-app-url` 과 대조하지 않는다(ALB 를
    따라가야 한다). 재배포 뒤 대상 앱 DB 신원 확인이 대신 잡는다.
