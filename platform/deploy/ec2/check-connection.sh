#!/usr/bin/env bash
# 환경 연결을 쓸 수 있는 상태인지 본다. 안 되면 이유를 한 줄로 stderr 에 내고 1 로 끝난다.
#   check-connection.sh <루트> <도메인> [기다릴 초] [기본 주소]
# 기본 주소를 생략하면 https://<도메인> 을 이 서버(127.0.0.1)로 붙어서 확인한다.
set -uo pipefail
root=${1:?루트}
domain=${2:?도메인}
wait_seconds=${3:-0}
base=${4:-}

curl_options=(-fsS --max-time 10)
if [[ -z "$base" ]]; then
  base="https://$domain"
  curl_options+=(--resolve "$domain:443:127.0.0.1")
fi

env_file="$root/connection/server.env"
broker_file="$root/connection/broker/broker.json"
for f in "$env_file" "$broker_file"; do
  [[ -s "$f" ]] || { echo "$f 가 없습니다" >&2; exit 1; }
done

read_env() { sed -n "s/^$1=//p" "$env_file" | tail -n 1; }
want_version=$(read_env ONEPORT_CONNECTION_RELEASE_VERSION)
want_sha=$(read_env ONEPORT_CONNECTION_BOOTSTRAP_SHA384)
read -r broker_version broker_sha < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("release_version", ""), d.get("bootstrap_sha384", ""))
' "$broker_file")

if [[ -z "$want_version" || -z "$want_sha" ]]; then
  echo "server.env 에 배포 버전·해시가 없습니다" >&2
  exit 1
fi
if [[ "$broker_version" != "$want_version" || "$broker_sha" != "$want_sha" ]]; then
  echo "중계 설정(broker.json)의 버전·해시가 API 설정(server.env)과 다릅니다" >&2
  exit 1
fi

failure=""
attempt() {
  local caps fields available reason version path sha
  if ! caps=$(curl "${curl_options[@]}" "$base/api/v1/connections/capabilities" 2>/dev/null); then
    failure="플랫폼 API 의 연결 기능 조회에 실패했습니다"
    return 1
  fi
  fields=$(python3 -c '
import json, sys
from urllib.parse import urlparse
d = json.loads(sys.stdin.read())
print("|".join([
    "yes" if d.get("available") is True else "no",
    (d.get("reason") or "").replace("|", "/"),
    d.get("releaseVersion") or "",
    urlparse(d.get("bootstrapUrl") or "").path,
]))
' <<<"$caps") || { failure="연결 기능 응답을 읽지 못했습니다"; return 1; }
  IFS='|' read -r available reason version path <<<"$fields"
  if [[ "$available" != yes ]]; then
    failure="연결 기능이 꺼져 있습니다: ${reason:-사유 없음}"
    return 1
  fi
  if [[ "$version" != "$want_version" ]]; then
    failure="API 가 안내하는 버전($version)이 설정($want_version)과 다릅니다"
    return 1
  fi
  if [[ -z "$path" ]] || ! sha=$(curl "${curl_options[@]}" "$base$path" 2>/dev/null \
      | python3 -c 'import hashlib, sys; print(hashlib.sha384(sys.stdin.buffer.read()).hexdigest())'); then
    failure="배포본 bootstrap.sh 를 받지 못했습니다"
    return 1
  fi
  if [[ "$sha" != "$want_sha" ]]; then
    failure="내려가는 bootstrap.sh 의 해시가 설정과 다릅니다"
    return 1
  fi
}

deadline=$((SECONDS + wait_seconds))
while true; do
  attempt && exit 0
  (( SECONDS >= deadline )) && break
  sleep 3
done
echo "$failure" >&2
exit 1
