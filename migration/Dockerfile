# 이전 도구 이미지: ghcr.io/softbankhackathon2026/migration-tools
# 모든 명령은 `python -m migration.<module>` 형태로 실행한다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MIGRATION_HOME=/app \
    WORKER_STATE_DIR=/var/lib/worker

WORKDIR /app

# 의존성만 먼저 설치해 레이어 캐시를 활용한다.
COPY pyproject.toml README.md ./
COPY migration ./migration
COPY sql ./sql
COPY connectors ./connectors
COPY config.example.yaml ./config.example.yaml

RUN pip install --no-cache-dir ".[policy-runtime]" \
    && groupadd --system --gid 10001 migration \
    && useradd --system --uid 10001 --gid migration --home-dir /app --shell /usr/sbin/nologin migration \
    && mkdir -p /var/lib/worker \
    && chown -R migration:migration /app /var/lib/worker

USER migration

EXPOSE 8080

ENTRYPOINT ["python", "-m"]
# 기본 CMD는 사용 가능한 명령 목록을 출력한다.
CMD ["migration"]
