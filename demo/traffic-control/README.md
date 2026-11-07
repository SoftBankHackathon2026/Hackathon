# demo/traffic-control — 데모 트래픽 제어

온프렘의 두 봇(INSERT 워커, traffic-bot)이 **새 요청을 만들지 여부**를 버튼으로 켜고 끈다.
이미 만든 요청은 끝까지 보낸다. 왜 이렇게 나눴는지는 [DESIGN.md](DESIGN.md).

```
server.js, lib/, public/   GKE 에 뜨는 상태판·명령 서버 (Node 22 내장 모듈만)
agent/agent.py             온프렘 에이전트 (파이썬 표준 라이브러리만, migration-tools 이미지에서 실행)
k8s/                       GKE 매니페스트. 온프렘 에이전트 매니페스트는 gitops apps/demo-traffic-agent
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

## 배포

GKE (`oneaction-dev`, 3분할 뷰어와 같은 클러스터):

```bash
TAG=$(git rev-parse HEAD)
IMAGE=asia-northeast3-docker.pkg.dev/ghost-project-513812/ghost/demo-traffic-control:$TAG
docker build --platform linux/amd64 -t $IMAGE demo/traffic-control && docker push $IMAGE
kubectl -n demo-traffic-control create secret generic demo-traffic-control-keys \
  --from-file=operator_key=/secure/operator_key --from-file=agent_key=/secure/agent_key
sed "s/IMAGE_TAG/$TAG/" demo/traffic-control/k8s/demo-traffic-control.yaml | kubectl apply -f -
```

온프렘 에이전트는 gitops `apps/demo-traffic-agent` 로 배포한다. 같은 `agent_key` 를 온프렘 Secret
`demo-traffic-agent` 에 넣는다.

## 버튼이 꺼지는 경우

| 화면 문구 | 이유 |
| --- | --- |
| 온프렘 에이전트 보고가 끊겼습니다 | 마지막 보고가 15초보다 오래됨 |
| 전환이 진행 중입니다 | 커넥터가 있거나, 끝나지 않은 전환 작업이 있거나, 워커가 SWITCHING |
| 시연 예산 모드 | 워커에 `max_generated` 가 있다. 생성량은 전환 도구가 정한다 |
| 미지원 버전 | 워커·봇 이미지에 생성 스위치가 아직 없다 |
| BOT_CONTROL_TOKEN 이 없습니다 | 에이전트 Secret 에 봇 제어 토큰이 없다 |
