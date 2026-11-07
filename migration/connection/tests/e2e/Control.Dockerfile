# Actual control plane, PostgreSQL/Redis adapters and production crypto gate.
FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends openssl ca-certificates \
    && openssl version | grep '^OpenSSL 3\.5\.' \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/source
COPY platform/backend/pyproject.toml /opt/source/backend/
COPY platform/backend/src /opt/source/backend/src/
COPY migration/connection/pyproject.toml migration/connection/__init__.py /opt/source/connection/
COPY migration/connection/control /opt/source/connection/control/
COPY migration/connection/contracts /opt/source/connection/contracts/
COPY migration/connection/platform_bridge /opt/source/connection/platform_bridge/
COPY migration/connection/inventory /opt/source/connection/inventory/
RUN pip install --no-cache-dir /opt/source/backend /opt/source/connection
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["uvicorn", "controlplane.main:app", "--host", "0.0.0.0", "--port", "8320", "--no-proxy-headers"]
