#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(cd "$here/../../../.." && pwd)
kubectl_version=${KUBECTL_VERSION:-v1.35.4}
[[ $EUID -eq 0 ]] || { echo "root 로 실행한다: sudo $0" >&2; exit 1; }
[[ -n "${K8S_API_SERVER:-}" ]] || { echo "K8S_API_SERVER (예: https://<API 주소>:6443) 가 필요하다" >&2; exit 1; }
arch=$(dpkg --print-architecture)

if [[ "$(/usr/local/bin/kubectl version --client -o json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["clientVersion"]["gitVersion"])' 2>/dev/null)" != "$kubectl_version" ]]; then
  tmp=$(mktemp -d)
  curl -fsSLo "$tmp/kubectl" "https://dl.k8s.io/release/$kubectl_version/bin/linux/$arch/kubectl"
  curl -fsSLo "$tmp/kubectl.sha256" "https://dl.k8s.io/release/$kubectl_version/bin/linux/$arch/kubectl.sha256"
  echo "$(cat "$tmp/kubectl.sha256")  $tmp/kubectl" | sha256sum --check --status
  install -m 0755 "$tmp/kubectl" /usr/local/bin/kubectl
  rm -rf "$tmp"
fi

id oneport-agent >/dev/null 2>&1 || useradd --system --home-dir /var/lib/oneport-agent --shell /usr/sbin/nologin oneport-agent
usermod -aG opensandbox oneport-agent
install -d -m 0750 -o oneport-agent -g oneport-agent /var/lib/oneport-agent
install -d -m 0750 -o root -g opensandbox /etc/oneport-agent
[[ -d /opt/oneport-agent/venv ]] || python3 -m venv /opt/oneport-agent/venv
/opt/oneport-agent/venv/bin/pip install -q --upgrade pip
/opt/oneport-agent/venv/bin/pip install -q "$repo/migration" "$repo/agent[opensandbox]"

if [[ ! -f /etc/oneport-agent/agent.yaml ]]; then
  sed "s#@K8S_API_SERVER@#$K8S_API_SERVER#" "$here/agent.yaml" >/etc/oneport-agent/agent.yaml
fi
while read -r service ports; do
  printf 'PORTS=%s\n' "$ports" >"/etc/oneport-agent/forward-$service.env"
done <"$here/forwards"
chown root:opensandbox /etc/oneport-agent/agent.yaml /etc/oneport-agent/forward-*.env
chmod 0640 /etc/oneport-agent/agent.yaml /etc/oneport-agent/forward-*.env

install -m 0644 "$here/oneport-forward@.service" /etc/systemd/system/oneport-forward@.service
install -m 0644 "$here/oneport-agent.service" /etc/systemd/system/oneport-agent.service
install -m 0755 "$here/oneport-agent" /usr/local/bin/oneport-agent
systemctl daemon-reload

missing=()
for f in kubeconfig kube-token kube-ca.crt db-ca.pem agent.env codex.env; do
  [[ -s /etc/oneport-agent/$f ]] || missing+=("$f")
done
if ((${#missing[@]})); then
  echo "아직 없는 파일: ${missing[*]} (/etc/oneport-agent, root:opensandbox 0640). README 의 '비밀 파일' 참고"
  echo "준비가 끝나면: sudo systemctl enable --now oneport-forward@{migration-policy,openbao,sample-app-smoke,postgres} oneport-agent"
  exit 0
fi
while read -r service _; do systemctl enable --now "oneport-forward@$service" >/dev/null; done <"$here/forwards"
systemctl enable --now oneport-agent >/dev/null
systemctl --no-pager --lines=0 status oneport-agent | head -3
