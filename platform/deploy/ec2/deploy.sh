#!/usr/bin/env bash
# EC2 에서 root 로 실행한다. CD 가 SSM 으로 compose.yaml·Caddyfile·fleet.json·cost.json·이 스크립트를 release 폴더에 풀고 부른다.
#   deploy.sh <release 폴더> <레지스트리> <이미지 태그>
# 새 버전이 pull·up --wait·/healthz 중 하나라도 실패하면 직전 태그와 설정 파일로 되돌려 다시 띄우고 0 이 아닌 값으로 끝난다.
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

# 레지스트리 로그인은 설정을 바꾸기 전에 한다. 여기서 실패하면 돌던 버전이 그대로 남는다.
aws ecr get-login-password --region "${AWS_REGION:-ap-northeast-2}" | docker login --username AWS --password-stdin "$registry" >/dev/null

# 덮어쓰기 전에 지금 돌고 있는 설정과 이미지 태그를 남겨 둔다. 새 버전이 뜨지 않으면 이것으로 되돌린다.
files=(compose.yaml Caddyfile fleet.json cost.json .env)
backup=$(mktemp -d "$root/.rollback.XXXXXX")
trap 'rm -rf "$backup"' EXIT
for f in "${files[@]}"; do
  if [[ -e "$root/$f" ]]; then cp -p "$root/$f" "$backup/$f"; fi
done
previous=$(sed -n 's/^ONEPORT_TAG=//p' "$backup/.env" 2>/dev/null || true)

rollback() {
  if [[ -z "$previous" ]]; then
    echo "이전 배포 기록(.env)이 없어 되돌리지 못합니다. 컨테이너 상태를 직접 확인합니다." >&2
    return
  fi
  echo "이전 버전 $previous 로 되돌립니다." >&2
  for f in "${files[@]}"; do
    if [[ -e "$backup/$f" ]]; then cp -p "$backup/$f" "$root/$f"; else rm -f "$root/$f"; fi
  done
  if docker compose up -d --remove-orphans --wait --wait-timeout 180; then
    echo "되돌리기 완료: $previous" >&2
  else
    echo "되돌린 버전 $previous 도 정상으로 뜨지 않았습니다. docker compose ps 로 직접 확인합니다." >&2
  fi
}

install -m 0644 "$release/compose.yaml" "$root/compose.yaml"
install -m 0644 "$release/Caddyfile" "$root/Caddyfile"
install -m 0644 "$release/fleet.json" "$root/fleet.json"
install -m 0644 "$release/cost.json" "$root/cost.json"
printf 'ONEPORT_REGISTRY=%s\nONEPORT_TAG=%s\n' "$registry" "$tag" >"$root/.env"

cd "$root"
if ! docker compose pull --quiet; then
  echo "배포 실패: $tag 이미지를 받지 못했습니다." >&2
  rollback
  exit 1
fi
if ! docker compose up -d --remove-orphans --wait --wait-timeout 180; then
  echo "배포 실패: $tag 컨테이너가 정상(healthy)으로 뜨지 않았습니다." >&2
  docker compose ps --all >&2 || true
  rollback
  exit 1
fi
if ! curl -fsS -o /dev/null --retry 12 --retry-delay 5 --retry-all-errors --resolve "$domain:443:127.0.0.1" "https://$domain/healthz"; then
  echo "배포 실패: $tag 에서 https://$domain/healthz 가 응답하지 않습니다." >&2
  rollback
  exit 1
fi
docker image prune -af --filter "until=168h" >/dev/null
echo "배포 완료: $tag"
