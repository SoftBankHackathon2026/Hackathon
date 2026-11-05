# Python 자동 계측 이미지

배포 요청에 `"instrumentation": "python"`을 넣으면, 플랫폼은 앱 코드를 바꾸지 않고 OpenTelemetry 계측을 붙입니다. 쿠버네티스 OpenTelemetry Operator의 자동 계측과 같은 방식입니다.

1. task에 init 컨테이너(`otel-python-init`)를 넣습니다. 이 이미지는 OpenTelemetry 패키지를 공유 볼륨 `/otel-auto-instrumentation-python`에 복사하고 끝납니다.
2. 앱 컨테이너는 init 컨테이너가 성공한 뒤 시작하고, `PYTHONPATH`에 계측 경로를 앞에 붙입니다. Python이 시작할 때 `sitecustomize.py`가 설치된 라이브러리(FastAPI, Flask, Django, SQLAlchemy, psycopg, Redis, requests, httpx 등)를 계측합니다.
3. trace는 같은 task의 collector 사이드카를 거쳐 X-Ray로 갑니다. trace ID는 X-Ray 형식으로 만들고(`OTEL_PYTHON_ID_GENERATOR=xray`), 로그 레코드에 `otelTraceID`가 붙어 플랫폼이 trace별 로그를 찾습니다.

환경에 trace 수집(`ONEPORT_TRACING=xray`)과 이 이미지(`ONEPORT_PYTHON_AUTOINSTRUMENTATION_IMAGE`)가 모두 설정되어야 배포 화면에 「Python 자동 계측」이 나옵니다.

## 빌드

amd64와 arm64 task 모두에서 쓰도록 두 아키텍처로 빌드해 ECR `oneport/otel-autoinstrumentation-python`에 올리고, digest를 플랫폼 AWS 설정에 넣습니다.

```bash
docker buildx build --platform linux/amd64,linux/arm64 --provenance=false \
  -t <계정>.dkr.ecr.ap-northeast-2.amazonaws.com/oneport/otel-autoinstrumentation-python:0.66b0-1 \
  --push platform/autoinstrumentation/python
```

```text
ONEPORT_PYTHON_AUTOINSTRUMENTATION_IMAGE=<계정>.dkr.ecr.ap-northeast-2.amazonaws.com/oneport/otel-autoinstrumentation-python@sha256:<digest>
```

패키지 버전은 [requirements.txt](requirements.txt)에서 함께 올립니다(SDK 1.45.0과 instrumentation 0.66b0). 이미지는 Python 3.13으로 빌드합니다. C 확장 패키지(wrapt 등)는 앱의 Python 버전이 다르면 순수 Python 구현으로 동작합니다.

## 제약

- 계측 패키지가 `PYTHONPATH` 앞에 오므로, 앱이 같은 패키지(예: `requests`, `protobuf`)의 다른 버전을 쓰면 이 이미지의 버전이 먼저 로드됩니다.
- 앱이 이미 OpenTelemetry SDK를 직접 설정했다면 자동 계측을 켜지 않습니다. 계측이 두 번 적용됩니다.
- 로그 연결은 앱 로그 형식이 로그 레코드의 추가 필드를 출력할 때만 동작합니다(JSON 로거 대부분).
