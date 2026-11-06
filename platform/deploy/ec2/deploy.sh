#!/usr/bin/env bash
# EC2 에서 root 로 실행한다. CD 가 SSM 으로 compose.yaml·Caddyfile·fleet.json·이 스크립트를 release 폴더에 풀고 부른다.
#   deploy.sh <release 폴더> <레지스트리> <이미지 태그>
set -euo pipefail
release=${1:?release 폴더}
registry=${2:?레지스트리}
tag=${3:?이미지 태그}
root=/opt/oneport

for f in secrets/postgres-password secrets/database-url secrets/ingest-token settings.env; do
  [[ -s "$root/$f" ]] || { echo "$root/$f 가 없습니다. platform/deploy/ec2/README.md 의 처음 한 번 준비를 먼저 합니다." >&2; exit 1; }
done
domain=$(sed -n 's/^ONEPORT_DOMAIN=//p' "$root/settings.env")
[[ -n "$domain" ]] || { echo "$root/settings.env 에 ONEPORT_DOMAIN 이 없습니다." >&2; exit 1; }

install -m 0644 "$release/compose.yaml" "$root/compose.yaml"
install -m 0644 "$release/Caddyfile" "$root/Caddyfile"
install -m 0644 "$release/fleet.json" "$root/fleet.json"
printf 'ONEPORT_REGISTRY=%s\nONEPORT_TAG=%s\n' "$registry" "$tag" >"$root/.env"

aws ecr get-login-password --region "${AWS_REGION:-ap-northeast-2}" | docker login --username AWS --password-stdin "$registry" >/dev/null
cd "$root"
docker compose pull --quiet
docker compose up -d --remove-orphans --wait --wait-timeout 180
curl -fsS -o /dev/null --retry 12 --retry-delay 5 --retry-all-errors --resolve "$domain:443:127.0.0.1" "https://$domain/healthz"
docker image prune -af --filter "until=168h" >/dev/null
echo "배포 완료: $tag"
