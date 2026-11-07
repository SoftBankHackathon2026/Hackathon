#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
version=${OPENSANDBOX_VERSION:-1.2.0}
codex_version=${CODEX_VERSION:-0.167.0}
image=${CODEX_IMAGE:-oneport/codex-sandbox}
[[ $EUID -eq 0 ]] || { echo "root 로 실행한다: sudo $0" >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu ]] || { echo "Ubuntu 만 지원한다" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive
arch=$(dpkg --print-architecture)

apt-get update -q
apt-get install -y -q ca-certificates curl gnupg python3-venv tinyproxy
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" >/etc/apt/sources.list.d/docker.list
curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor --yes -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$arch signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" >/etc/apt/sources.list.d/gvisor.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin runsc
install -D -m 0644 "$here/daemon.json" /etc/docker/daemon.json
systemctl enable docker >/dev/null
systemctl restart docker

docker network inspect sandbox-net >/dev/null 2>&1 || docker network create --driver bridge \
  --subnet 172.31.250.0/24 --gateway 172.31.250.1 \
  -o com.docker.network.bridge.name=br-sandbox -o com.docker.network.bridge.enable_icc=false sandbox-net >/dev/null
install -m 0755 "$here/sandbox-egress-rules" /usr/local/sbin/sandbox-egress-rules
install -m 0644 "$here/systemd/sandbox-egress.service" /etc/systemd/system/sandbox-egress.service
install -m 0644 "$here/tinyproxy.conf" /etc/tinyproxy/tinyproxy.conf
install -m 0644 "$here/tinyproxy.filter" /etc/tinyproxy/filter
install -D -m 0644 "$here/systemd/tinyproxy-after-docker.conf" /etc/systemd/system/tinyproxy.service.d/after-docker.conf
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
  ufw allow in on br-sandbox from 172.31.250.0/24 to 172.31.250.1 port 8888 proto tcp comment 'sandbox proxy' >/dev/null
fi

id opensandbox >/dev/null 2>&1 || useradd --system --home-dir /var/lib/opensandbox --shell /usr/sbin/nologin opensandbox
usermod -aG docker opensandbox
install -d -m 0750 -o opensandbox -g opensandbox /var/lib/opensandbox
install -d -m 0750 -o root -g opensandbox /etc/opensandbox /etc/oneport-agent
[[ -d /opt/opensandbox/venv ]] || python3 -m venv /opt/opensandbox/venv
/opt/opensandbox/venv/bin/pip install -q --upgrade pip
/opt/opensandbox/venv/bin/pip install -q "opensandbox-server==$version"
sed "s/@VERSION@/$version/g" "$here/config.toml" >/etc/opensandbox/config.toml
chown root:opensandbox /etc/opensandbox/config.toml
chmod 0640 /etc/opensandbox/config.toml
if [[ ! -s /etc/opensandbox/server.env ]]; then
  (umask 077; printf 'OPENSANDBOX_SERVER_API_KEY=%s\n' "$(openssl rand -hex 32)" >/etc/opensandbox/server.env)
fi
chown root:opensandbox /etc/opensandbox/server.env
chmod 0640 /etc/opensandbox/server.env
install -m 0644 "$here/systemd/opensandbox.service" /etc/systemd/system/opensandbox.service

docker pull -q "opensandbox/execd:release-$version" >/dev/null
docker build -q --build-arg "CODEX_VERSION=$codex_version" -t "$image:$codex_version" -t "$image:latest" \
  -f "$here/Dockerfile.codex" "$here" >/dev/null

systemctl daemon-reload
systemctl enable sandbox-egress tinyproxy opensandbox >/dev/null
systemctl restart sandbox-egress tinyproxy opensandbox
for _ in $(seq 1 30); do curl -fsS -o /dev/null http://127.0.0.1:8090/health 2>/dev/null && break; sleep 2; done
curl -fsS http://127.0.0.1:8090/health
echo
echo "Codex 키: /etc/oneport-agent/codex.env 에 CODEX_API_KEY=... (root:opensandbox 0640)"
