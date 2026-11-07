# 운영자 PC 전환 연결 예시

클라우드 간 전환을 운영자 PC 에서 돌릴 때 띄우는 것들의 예시다. 원가연
`migration.cloud` 가 전환 순서를 정하고, crosscloud 는 인계 파일과 앱 제어
어댑터만 맡는다. 요청은 접수 큐(durable-ingress)가 받으므로 전환 중에도
접수는 거절되지 않고, 앱으로 전달만 멈춘다.

## 구성

| 무엇 | 어디 | compose 프로젝트 |
|---|---|---|
| 접수 큐 durable-ingress + Redis | `ingress.compose.yaml` | `ccx-ingress` |
| Kafka + Connect(Debezium) | `connect.compose.yaml` | `ccx-connect` |
| 앱 제어 어댑터 | `crosscloud serve-adapter`(호스트 프로세스) | — |
| 전환 상태 머신 | `python -m migration.cloud`(호스트 프로세스) | — |

두 compose 프로젝트를 나눈 이유: 승인 전에 취소하면 Connect 만 내려도 접수
기록(Redis AOF)이 남는다. 모든 포트는 호스트 `127.0.0.1` 에만 공개한다.

## 순서

1.  인계 파일을 만든다. readiness 네 항목이 준비되면
    `handoff.render_cloud_config` 가 `out/config.cloud.yaml` 을,
    `handoff.render_ingress_env` 가 `out/ingress.env` 를 쓴다. 두 함수가
    돌려준 env 이름(비밀번호·제어 토큰)을 운영자 셸에서 Secret 으로 채운다.
    접수 큐가 원본을 가리키는지는 렌더링 조건이 아니다(이 다음 단계에서
    맞추고, 원가연 PRECHECK 가 확인한다).
1.  접수 큐를 띄운다.

    ```shell
    docker compose -f ingress.compose.yaml up -d
    ```

1.  접수 큐를 원본 라벨로 돌리고 전달을 연다. durable-ingress 는 처음에
    `onprem` 대상·paused 로 뜬다(`ingress.env` 의 `onprem` 은 원본 앱 별칭).
    원가연 `cloud.lock` 을 쥔 채 `--state-dir` 의 단계를 읽어 승인 전일 때만
    한다. 원가연을 아직 한 번도 돌리지 않아 상태 파일이 없으면
    `--allow-no-state` 를 준다(디렉토리는 미리 만든다).

    ```shell
    mkdir -p out/state
    crosscloud ingress-start --cloud-config out/config.cloud.yaml \
        --ingress-env out/ingress.env \
        --ingress-control-url http://127.0.0.1:9092 \
        --state-dir out/state --allow-no-state
    ```

1.  Connect 를 띄우고, 어댑터를 연다(같은 셸에 `INGRESS_CONTROL_TOKEN`).

    ```shell
    docker compose -f connect.compose.yaml up -d --build
    crosscloud serve-adapter --cloud-config out/config.cloud.yaml \
        --ingress-env out/ingress.env \
        --ingress-control-url http://127.0.0.1:9092
    ```

1.  원가연 명령으로 전환한다. READY(종료 코드 3)에서 승인한다.

    ```shell
    python -m migration.cloud --config out/config.cloud.yaml \
        --state-dir out/state run
    python -m migration.cloud --config out/config.cloud.yaml \
        --state-dir out/state run --approve <승인 id>
    ```

## 어댑터가 하는 일

| 원가연 호출 | 어댑터 동작 |
|---|---|
| `GET /status` | 접수 큐 `mode` 를 `RUNNING`·`PAUSED` 로, 대상 이름을 라벨로 |
| `POST /pause` | 접수 큐 `/pause`, `settled:true` 확인 |
| `POST /drain` | 접수 큐가 paused·`in_flight` 0 이 될 때까지 대기 |
| `POST /target` | 대상 앱 `/readyz`·`/api/info` 로 대상 DB 신원 확인 뒤 `/target` |
| `POST /resume` | 큐가 대상 라벨일 때만, 최신 epoch 로 `/resume` |
| `GET /healthz` | 대상 앱 `/readyz` 연속 3 회(25초 안) |

## 정리

실험이 끝나면 만든 것만 지운다. 라벨 `com.crosscloud.bench` 로 남은 것을
확인한다.

```shell
docker compose -f connect.compose.yaml down -v
docker compose -f ingress.compose.yaml down -v
docker ps -a --filter label=com.crosscloud.bench
```

## 주의

*   `SAMPLE_APP_IMAGE` 는 팀 sample-app 이미지를 그대로 쓰되, 제어 바인드
    주소·토큰을 env 로 받는 커밋(4ecee72) 이후 빌드여야 한다. 그 전 이미지는
    제어 API 를 컨테이너 안 loopback 에만 열어 호스트에서 닿지 않는다.
*   대상 앱 요청 API 는 토큰 없이 연다(`REQUEST_API_TOKEN` 미설정 시 open).
    durable-ingress 는 토큰을 쓰는 대상에 https 만 허용하므로, http LB 대상은
    `token_env` 를 두지 않는다.
