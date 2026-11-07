FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MIGRATION_HOME=/app \
    WORKER_STATE_DIR=/var/lib/worker

WORKDIR /app

COPY migration/pyproject.toml migration/README.md ./
COPY migration/migration ./migration
COPY migration/sql ./sql
COPY migration/connectors ./connectors
COPY migration/config.example.yaml ./config.example.yaml
COPY crosscloud/pyproject.toml crosscloud/README.md /opt/crosscloud/
COPY crosscloud/src /opt/crosscloud/src
COPY crosscloud/schema /opt/crosscloud/schema

RUN pip install --no-cache-dir ".[policy-runtime]" \
    && pip install --no-cache-dir -e /opt/crosscloud \
    && groupadd --system --gid 10001 migration \
    && useradd --system --uid 10001 --gid migration --home-dir /app --shell /usr/sbin/nologin migration \
    && mkdir -p /var/lib/worker \
    && chown -R migration:migration /app /var/lib/worker

USER migration

ENTRYPOINT ["python", "-m"]
CMD ["migration"]
