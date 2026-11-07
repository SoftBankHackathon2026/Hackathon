# AI 에이전트 이미지: ghcr.io/softbankhackathon2026/ghost-agent
# 빌드 컨텍스트는 Hackathon 저장소 루트다 (migration 패키지를 함께 설치한다):
#   docker build -f agent/Dockerfile -t ghost-agent .
# Codex CLI 는 npm 패키지(@openai/codex)로 넣는다. 버전은 아래 ARG 로 고정하고 바꿀 때 이미지 digest 가 바뀐다.
FROM python:3.12-slim

ARG CODEX_VERSION=0.166.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENT_HOME=/app \
    AGENT_STATE_DIR=/var/lib/agent \
    CODEX_HOME=/var/lib/agent/codex-home

WORKDIR /app

# Node 는 Codex CLI 실행에만 쓴다. git 은 Codex 가 작업 디렉터리를 저장소로 보지 않도록 --skip-git-repo-check 로 돌리므로 불필요.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && apt-get purge -y npm && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY migration/pyproject.toml migration/README.md ./migration/
COPY migration/migration ./migration/migration
COPY agent/pyproject.toml agent/README.md ./agent/
COPY agent/agent ./agent/agent
COPY agent/agent.example.yaml ./agent/agent.example.yaml

RUN pip install --no-cache-dir ./migration "./agent[aws,slack]" \
    && groupadd --system --gid 10002 agent \
    && useradd --system --uid 10002 --gid agent --home-dir /app --shell /usr/sbin/nologin agent \
    && mkdir -p /var/lib/agent/codex-home \
    && chown -R agent:agent /app /var/lib/agent

USER agent

ENTRYPOINT ["python", "-m", "agent"]
CMD ["slack", "--config", "/etc/agent/agent.yaml"]
