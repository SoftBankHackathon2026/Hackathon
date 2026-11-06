# AI 설정 수정 에이전트 이미지: ghcr.io/softbankhackathon2026/ghost-agent
# 빌드 컨텍스트는 저장소 루트다 (migration 패키지를 함께 넣는다).
#   docker build -f agent/Dockerfile -t ghost-agent .
FROM python:3.12-slim

ARG TARGETARCH
ARG CODEX_VERSION=0.154.0
ARG CODEX_SHA256_AMD64=d7e18b2597ae8f242f5f31ee9e90deef48dbc9edd634d9868fb6435d08c07f02
ARG CODEX_SHA256_ARM64=583b48df32804213bdcd338c2e5adb06b34340821fa757a726cc0a524fa33c27

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY migration/pyproject.toml migration/README.md ./migration/
COPY migration/migration ./migration/migration
COPY agent/pyproject.toml ./agent/
COPY agent/agent ./agent/agent

# Codex CLI 는 정적 바이너리를 버전·해시 고정으로 받는다. Node 를 이미지에 넣지 않는다.
RUN python - <<'PY'
import hashlib, io, os, tarfile, urllib.request
arch = os.environ["TARGETARCH"]
triple = {"amd64": "x86_64", "arm64": "aarch64"}[arch] + "-unknown-linux-musl"
expected = os.environ[f"CODEX_SHA256_{arch.upper()}"]
url = f"https://github.com/openai/codex/releases/download/rust-v{os.environ['CODEX_VERSION']}/codex-{triple}.tar.gz"
raw = urllib.request.urlopen(url, timeout=120).read()
actual = hashlib.sha256(raw).hexdigest()
assert actual == expected, f"codex sha256 불일치: {actual}"
with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
    member = tar.extractfile(f"codex-{triple}")
    with open("/usr/local/bin/codex", "wb") as out:
        out.write(member.read())
os.chmod("/usr/local/bin/codex", 0o755)
PY

RUN pip install --no-cache-dir ./migration ./agent \
    && groupadd --system --gid 10001 agent \
    && useradd --system --uid 10001 --gid agent --home-dir /var/lib/agent --shell /usr/sbin/nologin agent \
    && mkdir -p /var/lib/agent \
    && chown -R agent:agent /var/lib/agent

USER agent

ENTRYPOINT ["python", "-m", "agent"]
CMD ["--help"]
