#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADMIN_KUBECONFIG="${KUBECONFIG:-$HOME/.kube/oneport-onprem.yaml}"
OUTPUT="${OUTPUT:-$ROOT/.local/hackathon.kubeconfig}"
CONTEXT_NAME="hackathon"

mkdir -p "$(dirname "$OUTPUT")"
umask 077

server="${SERVER_URL:-$(kubectl --kubeconfig "$ADMIN_KUBECONFIG" config view --minify -o jsonpath='{.clusters[0].cluster.server}')}"
ca_data="$(kubectl --kubeconfig "$ADMIN_KUBECONFIG" config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
token="$(kubectl --kubeconfig "$ADMIN_KUBECONFIG" -n hackathon get secret hackathon-user-token -o jsonpath='{.data.token}' | openssl base64 -d -A)"

kubectl config --kubeconfig "$OUTPUT" set-cluster onprem \
  --server="$server" \
  --certificate-authority=<(printf '%s' "$ca_data" | openssl base64 -d -A) \
  --embed-certs=true >/dev/null
kubectl config --kubeconfig "$OUTPUT" set-credentials hackathon-user --token="$token" >/dev/null
kubectl config --kubeconfig "$OUTPUT" set-context "$CONTEXT_NAME" \
  --cluster=onprem --user=hackathon-user --namespace=hackathon >/dev/null
kubectl config --kubeconfig "$OUTPUT" use-context "$CONTEXT_NAME" >/dev/null
chmod 600 "$OUTPUT"

echo "Namespace-scoped kubeconfig written to $OUTPUT"
