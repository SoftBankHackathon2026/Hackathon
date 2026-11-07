# Additive image: existing platform/backend/Dockerfile remains unchanged.
# Build context: repository root. PLATFORM_IMAGE must be a reviewed base image.
ARG PLATFORM_IMAGE
FROM ${PLATFORM_IMAGE}
USER root
COPY migration/connection/pyproject.toml migration/connection/__init__.py /opt/connection-source/
COPY migration/connection/contracts/*.py /opt/connection-source/contracts/
COPY migration/connection/control/*.py /opt/connection-source/control/
COPY migration/connection/inventory/*.py /opt/connection-source/inventory/
COPY migration/connection/platform_bridge/*.py /opt/connection-source/platform_bridge/
RUN /opt/venv/bin/python -m ensurepip \
    && /opt/venv/bin/python -m pip install --no-cache-dir /opt/connection-source \
    && rm -rf /opt/connection-source
USER 10001
# Only a trusted local reverse proxy may supply source addresses. Never use '*'.
CMD ["uvicorn", "controlplane.main:app", "--host", "0.0.0.0", "--port", "8320", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
