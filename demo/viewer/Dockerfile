# 3분할 뷰어 이미지. 외부 의존성이 없어 소스만 복사한다.
# 설정은 이미지에 굽지 않는다. 클러스터는 ConfigMap 을 /config 에 올린다.
#   docker build -t demo-viewer demo/viewer
#   docker run -p 5050:5050 -v "$PWD/demo/viewer/config.demo.json:/config/config.json:ro" demo-viewer
FROM node:22.23.1-alpine

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=5050 \
    VIEWER_HOSTED=1 \
    VIEWER_CONFIG=/config/config.json

WORKDIR /app
COPY server.js ./
COPY public ./public

USER node
EXPOSE 5050
CMD ["node", "server.js"]
