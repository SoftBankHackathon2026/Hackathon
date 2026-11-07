#!/usr/bin/env bash
# 파드와 같은 제약의 컨테이너 2개(agent + codex-runner)로 실제 Codex 사이드카 왕복을 확인한다. agent/ 에서 실행한다.
#   read -rs CODEX_API_KEY && export CODEX_API_KEY && dev/sidecar_check.sh
# 키는 값 없는 `-e CODEX_API_KEY` 로 환경에서만 넘긴다. 명령행·파일에 쓰지 않고, 컨테이너와 볼륨은 끝나면 지운다.
set -euo pipefail
: "${CODEX_API_KEY:?CODEX_API_KEY 환경변수가 필요하다}"
RUNNER=agent-sidecar-runner
VOLUME=agent-sidecar-work
IMAGE=ghost-agent:sidecar-check
# gitops apps/ai-agent/configmap.yaml 의 codex.model 과 맞춘다. 다른 모델은 CODEX_MODEL 로 시험한다.
export CODEX_MODEL="${CODEX_MODEL:-gpt-5.5}"
echo "--- 모델: $CODEX_MODEL"
RESTRICT=(--read-only --cap-drop ALL --security-opt no-new-privileges --user 10001:10001)
cleanup() { docker rm -f "$RUNNER" >/dev/null 2>&1 || true; docker volume rm "$VOLUME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker build -q -f Dockerfile -t "$IMAGE" .. >/dev/null
docker volume create "$VOLUME" >/dev/null
docker run --rm -v "$VOLUME:/work" --entrypoint sh python:3.12-slim -c 'chown 10001:10001 /work'
# 메모리는 gitops apps/ai-agent/deployment.yaml 의 codex-runner limits 와 맞춘다.
docker run -d --name "$RUNNER" "${RESTRICT[@]}" --memory 512m --memory-swap 512m --tmpfs /tmp:size=128m,uid=10001 -e TMPDIR=/tmp -e CODEX_API_KEY \
  -v "$VOLUME:/work" --entrypoint python "$IMAGE" -m agent.codex_runner --dir /work >/dev/null

status=0
docker run --rm -i "${RESTRICT[@]}" --tmpfs /tmp:size=16m,uid=10001 -e VAULT_TOKEN=agent-only-marker -e CODEX_MODEL \
  -v "$VOLUME:/work" --entrypoint python "$IMAGE" - <<'PY' || status=$?
import json
import os
from agent.config import CodexSettings
from agent.proposer import build_proposer
from agent.scope import check_proposal
current = {"DB_HOST": "postgres-wrong.hackathon.svc.cluster.local", "DB_PORT": "5432",
           "DB_NAME": "migration_demo", "DB_USER": "migration_app", "DB_PASSWORD": "must-not-reach-codex"}
target_db = {"host": "postgres.hackathon.svc.cluster.local", "port": 5432}
proposal, files = build_proposer(CodexSettings(runner="sidecar", shared_dir="/work", timeout=280, model=os.environ["CODEX_MODEL"])).propose(
    current=current, target_db=target_db,
    reasons=[{"code": "APP-01-TARGET-DB-UNREACHABLE", "message": "대상 앱이 대상 DB 에 연결하지 못한다"}])
rejections = [r.code for r in check_proposal(proposal, current=current, target_db=target_db, file_changes=files)]
print(json.dumps({"proposal": proposal.as_dict(), "file_changes": files, "rejections": rejections}, ensure_ascii=False, indent=1))
raise SystemExit(0 if proposal.changes and not rejections else 3)
PY

echo "--- runner 로그"
docker logs "$RUNNER" 2>&1 | tail -5 | cut -c1-300
echo "--- 사이드카 메모리 (limit 512MiB, oom_kill 은 0 이어야 한다. peak 에는 회수 가능한 파일 캐시가 포함된다)"
docker exec "$RUNNER" sh -c 'echo "peak=$(( $(cat /sys/fs/cgroup/memory.peak) / 1048576 ))MiB $(grep -E "^oom_kill " /sys/fs/cgroup/memory.events)"'
echo "--- 사이드카 환경의 비밀값 이름 (CODEX_API_KEY 만 있어야 한다)"
docker exec "$RUNNER" sh -c 'env | cut -d= -f1 | grep -E "VAULT|SLACK|DATABASE|TOKEN|CODEX" || true'
echo "--- 사이드카 SA 토큰 (없어야 한다)"
docker exec "$RUNNER" sh -c 'ls /var/run/secrets/kubernetes.io/serviceaccount 2>/dev/null || echo 없음'
[ "$status" -eq 0 ] && echo "결과: 통과" || echo "결과: 실패 (exit $status)"
exit "$status"
