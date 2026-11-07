# demo/traffic-control — 데모 트래픽 제어

온프렘의 두 봇(INSERT 워커, traffic-bot)이 **새 요청을 만들지 여부**를 버튼으로 켜고 끈다.
이미 만든 요청은 끝까지 보낸다. 왜 이렇게 나눴는지는 [DESIGN.md](DESIGN.md).

```
server.js, lib/, public/   상태판·명령 서버 (Node 22 내장 모듈만)
agent/agent.py             봇 스위치를 부르는 에이전트 (파이썬 표준 라이브러리만)
```

온프렘에서는 둘이 한 파드의 두 컨테이너로 돈다. 매니페스트는 gitops `apps/demo-traffic-control`
(이 디렉터리의 파일을 ConfigMap 으로 올린다. 고칠 때는 양쪽을 같이 바꾼다).

## 화면 열기 (온프렘)

```bash
kubectl --kubeconfig ~/.kube/oneport-onprem.yaml -n hackathon port-forward svc/demo-traffic-control 5060:80
# http://127.0.0.1:5060 → 화면 아래 "운영자 키" 칸에 Secret demo-traffic-control 의 operator_key 를 넣는다
```

## 로컬 실행

```bash
OPERATOR_KEY=$(openssl rand -hex 24) AGENT_KEY=$(openssl rand -hex 24) node demo/traffic-control/server.js
# http://127.0.0.1:5060  (키가 없으면 해당 역할 경로는 503 으로 닫힌다)
```

## 시험

```bash
node --test demo/traffic-control/test/server.test.js
python3 -I demo/traffic-control/agent/test_agent.py
```

두 시험은 같은 서명 벡터를 확인한다. 브라우저(`public/hmac.js`)·서버(`lib/auth.js`)·에이전트(`agent.py`)가
같은 서명을 만들어야 한다.

## 버튼이 꺼지는 경우

| 화면 문구 | 이유 |
| --- | --- |
| 온프렘 에이전트 보고가 끊겼습니다 | 마지막 보고가 15초보다 오래됨 |
| 전환이 진행 중입니다 | 커넥터가 있거나, 끝나지 않은 전환 작업이 있거나, 워커가 SWITCHING |
| 시연 예산 모드 | 워커에 `max_generated` 가 있다. 생성량은 전환 도구가 정한다 |
| 미지원 버전 | 워커·봇 이미지에 생성 스위치가 아직 없다 |
| BOT_CONTROL_TOKEN 이 없습니다 | Secret 에 봇 제어 토큰이 없다 |
