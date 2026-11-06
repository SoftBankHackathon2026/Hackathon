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

# AWS 라우터가 Tailscale 로 VPC 대역을 광고해서, 그대로 두면 EKS 비공개 엔드포인트·VPC DNS 가 Tailscale 로 빠진다.
cat >/etc/systemd/system/oneport-vpc-bypass.service <<'UNIT'
[Unit]
Description=Keep VPC traffic off Tailscale (EKS private endpoint, VPC DNS)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'ip rule del to 10.0.0.0/16 lookup main priority 5200 2>/dev/null; ip rule add to 10.0.0.0/16 lookup main priority 5200'
ExecStop=/sbin/ip rule del to 10.0.0.0/16 lookup main priority 5200

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now oneport-vpc-bypass.service

fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >>/etc/fstab

install -d -m 0750 /opt/oneport /opt/oneport/secrets
install -d -m 0750 -o 10001 -g 10001 /opt/oneport/kube
