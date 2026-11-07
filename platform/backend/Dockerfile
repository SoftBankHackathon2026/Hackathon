# Oneport 플랫폼 API·수집기 이미지. 같은 이미지에서 명령만 바꿔 수집기를 띄운다.
#   API     : uvicorn controlplane.main:app (기본 CMD)
#   수집기  : python -m controlplane.execution.fleet_collector
FROM ghcr.io/astral-sh/uv:0.7.14 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra aws --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra aws --no-editable

FROM python:3.12-slim
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin oneport
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
EXPOSE 8320
CMD ["uvicorn", "controlplane.main:app", "--host", "0.0.0.0", "--port", "8320", "--proxy-headers", "--forwarded-allow-ips", "*"]
