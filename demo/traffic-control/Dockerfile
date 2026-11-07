# 데모 트래픽 제어 서버 이미지. 외부 의존성이 없어 소스만 복사한다. 에이전트(agent/)는 넣지 않는다
# — 온프렘에서 migration-tools 이미지 + ConfigMap 으로 돈다.
#   docker build --platform linux/amd64 -t demo-traffic-control demo/traffic-control
#   docker run -p 5060:8080 -e OPERATOR_KEY=... -e AGENT_KEY=... demo-traffic-control
FROM node:22.23.1-alpine

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app
COPY server.js ./
COPY lib ./lib
COPY public ./public

USER node
EXPOSE 8080
CMD ["node", "server.js"]
