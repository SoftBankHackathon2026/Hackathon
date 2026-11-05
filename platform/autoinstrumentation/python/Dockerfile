# syntax=docker/dockerfile:1
# Init image for Python zero-code instrumentation (same layout as the
# OpenTelemetry Operator's autoinstrumentation-python image).
# The platform runs it before the app; it copies the packages to a shared
# volume and the app loads them through PYTHONPATH (sitecustomize.py).
ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS build
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --target /autoinstrumentation -r /tmp/requirements.txt \
    && find /autoinstrumentation -name '__pycache__' -prune -exec rm -rf {} +

FROM busybox:1.37
COPY --from=build /autoinstrumentation /autoinstrumentation
RUN chmod -R go+r /autoinstrumentation
CMD ["cp", "-r", "/autoinstrumentation/.", "/otel-auto-instrumentation-python"]
