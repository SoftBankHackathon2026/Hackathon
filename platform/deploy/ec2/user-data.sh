#!/bin/bash
# EC2 user-data (Amazon Linux 2023). Docker·compose·Tailscale·스왑을 깔고 /opt/oneport 를 만든다.
set -euxo pipefail
dnf install -y docker
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL https://github.com/docker/compose/releases/download/v2.29.7/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

dnf config-manager --add-repo https://pkgs.tailscale.com/stable/amazon-linux/2023/tailscale.repo
dnf install -y tailscale
systemctl enable --now tailscaled

fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >>/etc/fstab

install -d -m 0750 /opt/oneport /opt/oneport/secrets
install -d -m 0750 -o 10001 -g 10001 /opt/oneport/kube
