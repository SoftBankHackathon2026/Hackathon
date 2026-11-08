# 데모 트래픽 제어 앱 설계

> 2026-11-08: INSERT 워커가 퇴역했다(gitops #75). 지금 제어 대상은 traffic-bot 하나이고, 화면은 심사위원이
> 처음 봐도 알 수 있게 "요청이 흐르는 길"과 "지금 저장되는 DB"를 보여 준다. 아래 워커 관련 절은 기록으로 남긴다.

시연 때 두 봇(INSERT 워커, traffic-bot)을 버튼으로 켜고 끄는 작은 앱이다. 평소에는 두 봇을 멈춰 DB 가 쓸데없이
커지지 않게 하고, 시연 때는 "DB 에 직접 쓰는 내부 시스템"과 "HTTP 로 들어오는 사용자 요청" 두 종류의 쓰기가
전환을 어떻게 지나가는지 켜고 끄며 보여 준다.

## 1. 왜 필요한가

- 두 봇은 늘 돈다. 워커 초당 1건, traffic-bot 은 접수 큐를 거쳐 초당 약 1.7건(2026-11-07 return DB 증가량으로 추정).
  하루에 20만 행 넘게 쌓이고, 전환 때 초기 복사가 그만큼 길어진다(11-07 리허설 CATCHING_UP 5분, 31만 행).
- 3분할 화면을 본다고 행이 늘지는 않는다. 뷰어와 앱 `/api/live` 는 읽기만 한다.
- 지금 멈추는 방법은 두 봇 모두 "보내기 일시정지"뿐이다. 새 요청은 계속 만들어 대기열·장부에 쌓이고,
  워커는 대기열이 1시간을 넘기면 새 요청을 `queue_rejected` 로 남긴다. 시연 지표에 실패가 섞인다.

## 2. 결정 요약

| 항목 | 결정 |
| --- | --- |
| 멈춤의 뜻 | **요청 생성만 끈다.** 이미 만든 요청은 끝까지 보낸다. 보내기 일시정지(`/pause`)는 쓰지 않는다 |
| 위치 | 온프렘 k3s `hackathon` 네임스페이스. 화면 서버와 에이전트를 **한 파드**에 둔다 |
| 접속 | 공개 주소 없음(ClusterIP). 운영자가 `kubectl port-forward` 로 연다. 온프렘 제어 API 는 밖에 열지 않는다 |
| 인증 | 운영자·에이전트 모두 공유 키 HMAC 서명. 키는 전송하지 않는다 |
| 전환 중 | 진행 중인 전환이 있으면 버튼을 잠근다. cutover 가 쓰는 레버(워커 pause/resume·접수 큐)는 건드리지 않는다 |
| 화면 | 한국어·일본어 병기. 요청 흐름(봇 → 접수 큐 → 앱 → DB)과 접수 큐가 지금 넘기는 대상(온프렘 DB / AWS RDS)을 보여 준다. 운영자 키 입력은 접어 둔다 |
| 워커 시연 예산 모드 | `max_generated` 가 설정된 워커(총 1,000건 시연, 6d72cbf)는 cutover 가 생성량을 정한다. 앱은 **표시만** 한다 |

## 3. 구성

```
 운영자 Mac ── kubectl port-forward ──▶ 온프렘 k3s hackathon / 파드 demo-traffic-control
                                         ├ server 컨테이너 (server.js, node 이미지)
                                         │   GET  /api/state       누구나 읽기 (상태판)
                                         │   POST /api/commands    운영자 서명 필요
                                         │   GET  /agent/commands  에이전트 서명 필요 ◀─┐ 127.0.0.1, 2초마다
                                         │   POST /agent/report    에이전트 서명 필요 ◀─┤
                                         └ agent 컨테이너 (agent.py, migration-tools 이미지) ┘
      agent →  GET  traffic-bot-control:9091/control
               POST traffic-bot-control:9091/control/generation {enabled} (Bearer)
               GET  durable-ingress-control:9092/status     (지금 저장 대상 표시, 읽기만)
               GET  migration-connect:8083/connectors       (전환 잠금 판단)
               워커 PVC db-insert-worker-state/cutover.sqlite3 를 mode=ro 로 (전환 잠금 판단)
```

서버는 상태를 메모리에만 둔다(복제본 1개). 재시작하면 명령 대기열이 비고, 에이전트의 다음 보고로 상태판이 채워진다.
봇의 실제 상태는 봇 자신이 들고 있으므로 서버가 날아가도 봇은 마지막 상태를 유지한다.

## 4. 봇별 제어 계약

### traffic-bot (sample-app `packages/traffic-bot`)

- 기존 metrics 포트(9091)에 제어 경로를 더한다.
  - `GET /control` → `{generation: "running"|"stopped", pending, committed, failed, rate_per_second, run_id}`
  - `POST /control/generation` `{"enabled": bool}` → 같은 응답. `Authorization: Bearer <BOT_CONTROL_TOKEN>` 필요
- `BOT_CONTROL_TOKEN` 이 없으면 제어 경로는 403 으로 닫는다(기존 배포는 동작이 바뀌지 않는다).
- `BOT_GENERATION=stopped` 로 시작 상태를 정한다. 기본은 `running`(기존과 같음).
- 생성만 멈추고 배달(재시도·확인)은 계속한다. 장부·run_id 는 그대로라 다시 켜면 번호가 이어진다.
- 재시작하면 `BOT_GENERATION` 값으로 돌아간다. 평소 멈춰 두려면 gitops 에서 `BOT_GENERATION=stopped` 로 둔다.

### INSERT 워커 (Hackathon `migration/loadgen.py`) — 2026-11-08 퇴역, 제어 대상에서 뺐다

- 연속 모드(`max_generated` 없음)에만 `POST /generation {"enabled": bool}` 을 더한다. 생성만 멈추고 대기열 전송은 계속한다.
  값은 `worker.json` 에 저장해 liveness 재시작 뒤에도 유지한다.
- 시연 예산 모드(`max_generated` 있음)에서는 409 로 거절한다. 생성량은 cutover 의 `/generation-limit` 이 정한다.
- `/status` 에 `generation_enabled` 를 더한다. 에이전트는 이 필드가 없으면 "이 버전은 미지원"으로 표시한다.

## 5. 전환 중 잠금

에이전트가 매 보고마다 계산하고, 서버도 마지막 보고 기준으로 한 번 더 막는다. 아래 중 하나라도 맞으면 잠근다.

1. Kafka Connect 에 커넥터가 하나라도 있다.
2. `cutover.sqlite3` 에 종료되지 않은 작업이 있다. 종료는 `COMPLETED`·`ABORTED`·`ROLLED_BACK`, 그리고
   `manual_resolution` 기록이 붙은 작업이다. 해소 기록 없는 `NEEDS_RECOVERY` 도 잠근다(사람이 확정 전).
3. 에이전트 보고가 15초 넘게 끊겼다(서버 판단).

접수 큐 대상(`/status`)은 표시용이라 읽기에 실패해도 잠그지 않는다.

이유: cutover 는 CATCHING_UP 에서 원본에 100건이 더 쌓이길, VERIFYING 에서 대상에 100건이 커밋되길 기다린다.
그 사이 워커 생성을 끄면 단계가 시간 초과로 실패한다.

## 6. 인증

- 요청 헤더 `X-Demo-Auth: <role>.<unix초>.<nonce>.<hex 서명>`
- 서명 = HMAC-SHA256(키, `role \n METHOD \n path \n unix초 \n nonce \n sha256(body)`)
- 서버는 ±30초 밖 시각과 이미 본 nonce 를 거절한다. 역할은 `operator`, `agent` 로 키가 다르다.
- 브라우저 서명은 작은 SHA-256/HMAC 구현(`public/hmac.js`)으로 한다. WebCrypto 는 HTTPS·localhost 에서만 쓸 수 있어,
  어떤 주소로 열어도 같은 코드가 돌게 했다. 운영자 키는 탭의 sessionStorage 에만 두고 전송하지 않는다.
- 같은 네임스페이스의 파드는 서로 닿는다(`allow-same-namespace`). 그래서 파드 안 연결이라도 서명을 유지한다.

## 7. 온프렘 에이전트

- `migration-tools` 이미지 + ConfigMap 스크립트(파이썬 표준 라이브러리만). 플랫폼 보고기와 같은 방식이라 새 이미지가 없다.
  서버도 공개 `node` 이미지 + ConfigMap 으로 올린다(온프렘은 GKE Artifact Registry 비공개 이미지를 받지 못한다).
- 워커 PVC 를 붙이고 워커와 같은 노드에 둔다(RWO local-path). SQLite 는 `mode=ro` 로만 연다. 마운트를 readOnly 로
  하면 WAL DB 를 열고 있는 프로세스가 없을 때 `-shm` 을 못 만들어 열리지 않는다(컨테이너로 확인).
- 명령은 한 번에 하나씩 실행하고 결과(성공·거절 사유)를 다음 보고에 담는다. 명령을 스스로 재시도하지 않는다.
- 에이전트는 상태를 "맞추지" 않는다. 사람이 누른 명령만 실행한다(cutover 가 바꾼 상태와 다투지 않으려고).

## 8. 배포

| 무엇 | 어디 | 방법 |
| --- | --- | --- |
| server.js·lib·public·agent.py | 온프렘 k3s `hackathon`, 파드 하나 | gitops `apps/demo-traffic-control` + `argocd/onprem/demo-traffic-control.yaml`, 키는 손으로 만든 Secret `demo-traffic-control` |
| traffic-bot 제어 경로 | sample-app PR → CI 이미지 → gitops 태그 | 제어 Service `traffic-bot-control:9091`, `BOT_CONTROL_TOKEN` Secret |
| 워커 생성 스위치 | Hackathon #142 (머지됨) | 워커 퇴역으로 지금은 쓰지 않는다 |

봇 쪽 변경이 배포되기 전에는 앱이 그 봇을 "미지원"으로 표시하고 버튼을 끈다. 앱과 에이전트는 먼저 띄울 수 있다.

## 9. 검토했지만 고르지 않은 안

- **3분할 뷰어에 버튼 추가**: 공개 주소에 QR 까지 띄우는 화면이다. 관객이 누를 수 있다.
- **봇 Deployment replicas 0/1 조절**: Argo selfHeal 이 되돌린다. 에이전트에 k8s 쓰기 권한도 줘야 한다.
- **워커 `/pause`·접수 큐 `/pause` 재사용**: cutover 가 쓰는 레버라 전환과 충돌하고, 멈춰도 요청이 쌓인다.
- **온프렘 제어 API 를 노드포트로 공개**: a88f4ab 에서 일부러 닫은 경로다.
- **화면을 GKE 에 띄우고 온프렘 에이전트가 바깥 방향으로 붙기**(처음 구현): 시연장에서 온프렘 접속이 안 될 때도
  화면은 열린다. 하지만 공개 IP·임시 IP·인터넷을 건너는 연결이 늘고, 에이전트는 어차피 온프렘에 있어야 한다.
  운영자 노트북이 온프렘에 닿는 전제로 온프렘 한 파드로 줄였다(2026-11-08).
- **DB 를 지우는 초기화 버튼**: 워커·봇 장부와 세 DB, seed 를 같이 맞춰야 해서 이 앱 범위 밖이다.

## 10. 남은 위험

- 서버 복제본 1개·메모리 상태: 재시작하면 처리 중 명령 결과를 잃을 수 있다. 봇 상태는 다음 보고로 다시 보인다.
- 운영자 노트북이 온프렘 클러스터에 닿지 않으면(시연장 네트워크·VPN) 화면을 열 수 없다.
- 봇은 traffic-bot 하나다. "봇 두 개로 상황 연출"은 워커 퇴역으로 할 수 없고, traffic-bot 을 켜고 끄며 접수 큐가 전환 중에도 요청을 받는 모습을 보여 준다.
