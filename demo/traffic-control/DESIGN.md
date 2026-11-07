# 데모 트래픽 제어 앱 설계

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
| 화면·API 위치 | GKE `oneaction-dev` 의 `demo-traffic-control` 네임스페이스 (3분할 뷰어와 같은 클러스터) |
| 온프렘 연결 | 온프렘 에이전트가 GKE 로 **바깥 방향으로만** 붙는다. 온프렘 제어 API 는 밖에 열지 않는다 |
| 인증 | 운영자·에이전트 모두 공유 키 HMAC 서명. 키는 전송하지 않는다(평문 HTTP 구간 대비) |
| 전환 중 | 진행 중인 전환이 있으면 버튼을 잠근다. cutover 가 쓰는 레버(워커 pause/resume·접수 큐)는 건드리지 않는다 |
| 워커 시연 예산 모드 | `max_generated` 가 설정된 워커(총 1,000건 시연, 6d72cbf)는 cutover 가 생성량을 정한다. 앱은 **표시만** 한다 |

## 3. 구성

```
 운영자 브라우저 ──HTTP──▶ GKE demo-traffic-control (server.js)
                            │  GET  /api/state            누구나 읽기 (상태판)
                            │  POST /api/commands         운영자 서명 필요
                            │  GET  /agent/commands       에이전트 서명 필요 ◀──┐
                            │  POST /agent/report         에이전트 서명 필요 ◀──┤ 2초마다 바깥 방향 HTTPS/HTTP
                                                                                │
 온프렘 k3s hackathon ─ demo-traffic-agent (agent.py, migration-tools 이미지) ───┘
      │ GET  db-insert-worker:8080/status (10초마다, 부하 제한)
      │ POST db-insert-worker:8080/generation        {enabled}   ← 연속 모드에서만
      │ GET  traffic-bot-control:9091/control
      │ POST traffic-bot-control:9091/control/generation {enabled} (Bearer)
      │ GET  migration-connect:8083/connectors       (전환 잠금 판단)
      └ 읽기 전용 PVC db-insert-worker-state/cutover.sqlite3 (전환 잠금 판단)
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

### INSERT 워커 (Hackathon `migration/loadgen.py`)

- 연속 모드(`max_generated` 없음)에만 `POST /generation {"enabled": bool}` 을 더한다. 생성만 멈추고 대기열 전송은 계속한다.
  값은 `worker.json` 에 저장해 liveness 재시작 뒤에도 유지한다.
- 시연 예산 모드(`max_generated` 있음)에서는 409 로 거절한다. 생성량은 cutover 의 `/generation-limit` 이 정한다.
- `/status` 에 `generation_enabled` 를 더한다. 에이전트는 이 필드가 없으면 "이 버전은 미지원"으로 표시한다.

## 5. 전환 중 잠금

에이전트가 매 보고마다 계산하고, 서버도 마지막 보고 기준으로 한 번 더 막는다. 아래 중 하나라도 맞으면 잠근다.

1. Kafka Connect 에 커넥터가 하나라도 있다.
2. `cutover.sqlite3` 에 종료되지 않은 작업이 있다. 종료는 `COMPLETED`·`ABORTED`·`ROLLED_BACK`, 그리고
   `manual_resolution` 기록이 붙은 작업이다. 해소 기록 없는 `NEEDS_RECOVERY` 도 잠근다(사람이 확정 전).
3. 워커 상태가 `SWITCHING` 이다.
4. 에이전트 보고가 15초 넘게 끊겼다(서버 판단).

이유: cutover 는 CATCHING_UP 에서 원본에 100건이 더 쌓이길, VERIFYING 에서 대상에 100건이 커밋되길 기다린다.
그 사이 워커 생성을 끄면 단계가 시간 초과로 실패한다.

## 6. 인증

- 요청 헤더 `X-Demo-Auth: <role>.<unix초>.<nonce>.<hex 서명>`
- 서명 = HMAC-SHA256(키, `role \n METHOD \n path \n unix초 \n nonce \n sha256(body)`)
- 서버는 ±30초 밖 시각과 이미 본 nonce 를 거절한다. 역할은 `operator`, `agent` 로 키가 다르다.
- 브라우저는 평문 HTTP 에서 WebCrypto 를 못 쓰므로 작은 SHA-256/HMAC 구현(`public/hmac.js`)을 둔다.
  운영자 키는 탭의 sessionStorage 에만 두고 전송하지 않는다.
- 한계: 평문 HTTP 라 상태판 내용과 명령 본문은 보인다(비밀값은 없다). HTTPS 는 도메인 확정 뒤 후속.

## 7. 온프렘 에이전트

- `migration-tools` 이미지 + ConfigMap 스크립트(파이썬 표준 라이브러리만). 플랫폼 보고기와 같은 방식이라 새 이미지가 없다.
- 워커 PVC 를 **읽기 전용**으로 붙이고 워커와 같은 노드에 둔다(RWO local-path).
- 워커 `/status` 는 10초에 한 번만 부른다. 11-07 워커가 `/status` 부하 중 liveness 로 재시작된 일이 있다.
- 명령은 한 번에 하나씩 실행하고 결과(성공·거절 사유)를 다음 보고에 담는다. 명령을 스스로 재시도하지 않는다.
- 에이전트는 상태를 "맞추지" 않는다. 사람이 누른 명령만 실행한다(cutover 가 바꾼 상태와 다투지 않으려고).

## 8. 배포

| 무엇 | 어디 | 방법 |
| --- | --- | --- |
| server.js + public | GKE `oneaction-dev` / `demo-traffic-control` | Artifact Registry 이미지 + `k8s/demo-traffic-control.yaml`, 키는 손으로 만든 Secret |
| agent.py | 온프렘 k3s `hackathon` | gitops `apps/demo-traffic-agent` + `argocd/onprem/demo-traffic-agent.yaml` |
| traffic-bot 제어 경로 | sample-app PR → CI 이미지 → gitops 태그 | 제어 Service `traffic-bot-control:9091`, `BOT_CONTROL_TOKEN` Secret |
| 워커 생성 스위치 | Hackathon PR → migration 이미지 → gitops 태그 | 가연님 리뷰(시연 예산 모드와 같은 파일) |

봇 쪽 변경이 배포되기 전에는 앱이 그 봇을 "미지원"으로 표시하고 버튼을 끈다. 앱과 에이전트는 먼저 띄울 수 있다.

## 9. 검토했지만 고르지 않은 안

- **3분할 뷰어에 버튼 추가**: 공개 주소에 QR 까지 띄우는 화면이다. 관객이 누를 수 있다.
- **봇 Deployment replicas 0/1 조절**: Argo selfHeal 이 되돌린다. 에이전트에 k8s 쓰기 권한도 줘야 한다.
- **워커 `/pause`·접수 큐 `/pause` 재사용**: cutover 가 쓰는 레버라 전환과 충돌하고, 멈춰도 요청이 쌓인다.
- **온프렘 제어 API 를 노드포트로 공개**: a88f4ab 에서 일부러 닫은 경로다.
- **DB 를 지우는 초기화 버튼**: 워커·봇 장부와 세 DB, seed 를 같이 맞춰야 해서 이 앱 범위 밖이다.

## 10. 남은 위험

- 서버 복제본 1개·메모리 상태: 재시작하면 처리 중 명령 결과를 잃을 수 있다. 봇 상태는 다음 보고로 다시 보인다.
- 평문 HTTP(6장 한계).
- 시연 예산 모드에서는 워커를 끌 수 없다. 그 시연에서 "봇 2개 연출"은 traffic-bot 만 켜고 끄는 것으로 한다.
