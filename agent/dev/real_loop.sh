#!/usr/bin/env bash
# 실제 Codex 로컬 한 바퀴. 컨테이너 3개를 띄우고 끝나면 지운다. agent/ 에서 실행한다.
#   dev/real_loop.sh                     # CODEX_API_KEY 사용
#   CODEX_HOME_DIR=~/.codex dev/real_loop.sh --no-auto-apply
set -euo pipefail
PREFIX=agent-local
TOKEN=local-dev-root
PGPASSWORD_LOCAL=local-only
cleanup() { docker rm -f "$PREFIX-opa" "$PREFIX-bao" "$PREFIX-pg" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker run -d --name "$PREFIX-opa" -p 127.0.0.1:18181:8181 -v "$PWD/../policies:/p:ro" \
  openpolicyagent/opa:1.22.0-static run --server --addr :8181 /p/migration.rego >/dev/null
docker run -d --name "$PREFIX-bao" -p 127.0.0.1:18200:8200 --cap-add IPC_LOCK \
  -e BAO_DEV_ROOT_TOKEN_ID="$TOKEN" -e BAO_DEV_LISTEN_ADDRESS=0.0.0.0:8200 \
  quay.io/openbao/openbao:2.7.2 server -dev >/dev/null
docker run -d --name "$PREFIX-pg" -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD="$PGPASSWORD_LOCAL" \
  -e POSTGRES_USER=migration_app -e POSTGRES_DB=migration_demo postgres:16-alpine >/dev/null

for _ in $(seq 1 60); do
  curl -fsS -o /dev/null http://127.0.0.1:18181/health \
    && curl -fsS -o /dev/null http://127.0.0.1:18200/v1/sys/health \
    && docker exec "$PREFIX-pg" pg_isready -q -h 127.0.0.1 -U migration_app -d migration_demo \
    && break
  sleep 1
done

# 장애 상태: 존재하지 않는 호스트
curl -fsS -o /dev/null -H "X-Vault-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"data":{"DB_HOST":"postgres-wrong.invalid","DB_PORT":"55432","DB_NAME":"migration_demo","DB_USER":"migration_app"}}' \
  http://127.0.0.1:18200/v1/secret/data/hackathon/sample-app/onprem

export SAMPLE_APP_DATABASE_URL="postgresql://migration_app:$PGPASSWORD_LOCAL@localhost:55432/migration_demo"
HOME_ARG=()
[ -n "${CODEX_HOME_DIR:-}" ] && HOME_ARG=(--codex-home "$CODEX_HOME_DIR")
status=0
"${PYTHON:-python}" dev/real_loop.py --opa-url http://127.0.0.1:18181 --bao-addr http://127.0.0.1:18200 --bao-token "$TOKEN" \
  ${HOME_ARG[@]+"${HOME_ARG[@]}"} "$@" || status=$?
echo "--- OpenBao 최종 값"
curl -fsS -H "X-Vault-Token: $TOKEN" http://127.0.0.1:18200/v1/secret/data/hackathon/sample-app/onprem \
  | "${PYTHON:-python}" -c 'import json,sys; d=json.load(sys.stdin)["data"]; print(d["metadata"]["version"], d["data"])'
exit $status
