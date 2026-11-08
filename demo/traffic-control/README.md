# demo/traffic-control — 데모 트래픽 제어

온프렘 traffic-bot 이 **새 요청을 만들지 여부**를 버튼으로 켜고 끈다(INSERT 워커는 2026-11-08 퇴역).
이미 만든 요청은 끝까지 보낸다. 왜 이렇게 나눴는지는 [DESIGN.md](DESIGN.md).

```
server.js, lib/, public/   상태판·명령 서버 (Node 22 내장 모듈만)
agent/agent.py             봇 스위치를 부르고 접수 큐 대상을 읽는 에이전트 (파이썬 표준 라이브러리만)
```

온프렘에서는 둘이 한 파드의 두 컨테이너로 돈다. 매니페스트는 gitops `apps/demo-traffic-control`
(이 디렉터리의 파일을 ConfigMap 으로 올린다. 고칠 때는 양쪽을 같이 바꾼다).

## 화면 열기 (온프렘)

```bash
kubectl --kubeconfig ~/.kube/oneport-onprem.yaml -n hackathon port-forward svc/demo-traffic-control 8095:80
# http://127.0.0.1:8095 → 화면 아래 "운영자 전용"을 펼쳐 "운영자 키" 칸에 Secret demo-traffic-control 의 operator_key 를 넣는다
```

8095 를 쓰는 이유: Chrome 은 5060(SIP) 같은 포트를 ERR_UNSAFE_PORT 로 막는다.

## 로컬 실행

```bash
OPERATOR_KEY=$(openssl rand -hex 24) AGENT_KEY=$(openssl rand -hex 24) node demo/traffic-control/server.js
# http://127.0.0.1:8095  (키가 없으면 해당 역할 경로는 503 으로 닫힌다)
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
| 온프렘과 연결이 끊겨 지금은 누를 수 없습니다 | 마지막 보고가 15초보다 오래됨 |
| DB 이전이 진행 중이라 잠겨 있습니다 | 커넥터가 있거나, 끝나지 않은 전환 작업이 있다 |
| 봇이 아직 켜고 끄기를 지원하지 않습니다 | 봇 이미지에 생성 스위치가 없다 |
| BOT_CONTROL_TOKEN 이 없습니다 | Secret 에 봇 제어 토큰이 없다 |
