#!/usr/bin/env bash
# 전체 적용 순서. 매니페스트를 고친 뒤에도 같은 명령을 다시 돌린다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/oneport-onprem.yaml}"
K=(kubectl --kubeconfig "$KUBECONFIG_PATH")
H=(helm --kubeconfig "$KUBECONFIG_PATH")

# 1. Argo CD: 기본 값(argocd-values.base.yaml) + 알림·metrics·Ingress(argo.yubin.dev) + observer 로그인
"$ROOT/bootstrap-secrets.sh" argocd-login
"${H[@]}" repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
"${H[@]}" repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
"${H[@]}" repo update argo prometheus-community >/dev/null
# 레거시 수동 Ingress(argocd.yubin.website) 는 차트 Ingress 와 이름이 같아 Helm 이 넘겨받지 못한다. 한 번만 지운다.
if [[ "$("${K[@]}" -n argocd get ingress argocd-server -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null)" != "Helm" ]]; then
  "${K[@]}" -n argocd delete ingress argocd-server --ignore-not-found >/dev/null
fi
"${H[@]}" -n argocd upgrade --install argocd argo/argo-cd --version 10.9.6 --create-namespace --reset-values \
  -f "$ROOT/admin/argocd-values.base.yaml" -f "$ROOT/admin/argocd-values.yaml" \
  -f "$ROOT/.local/argocd-login.values.yaml" --wait --timeout 5m >/dev/null

# 1b. Argo Rollouts 컨트롤러 (공식 v1.10.0 매니페스트)
"${K[@]}" apply --server-side --force-conflicts -k "$ROOT/admin/argo-rollouts" >/dev/null

# 2. 시크릿 (Argo CD 업그레이드 뒤: 차트가 더 이상 argocd-notifications-secret 을 소유하지 않을 때 만든다)
"$ROOT/bootstrap-secrets.sh"

# 3. cert-manager (Argo CD UI 인증서, Route 53 DNS-01). CRD 가 생긴 뒤 ClusterIssuer 를 적용한다
"${H[@]}" repo add jetstack https://charts.jetstack.io >/dev/null 2>&1 || true
"${H[@]}" repo update jetstack >/dev/null
"${H[@]}" -n cert-manager upgrade --install cert-manager jetstack/cert-manager \
  --version v1.21.3 --create-namespace -f "$ROOT/admin/cert-manager/values.yaml" --wait --timeout 5m >/dev/null
"${K[@]}" apply -k "$ROOT/admin/cert-manager"

# 4. 모니터링 (CRD 가 생겨야 ServiceMonitor 를 적용할 수 있다)
"${H[@]}" -n monitoring upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  --version 91.9.0 --create-namespace -f "$ROOT/admin/monitoring/values.yaml" --wait --timeout 10m >/dev/null
"${K[@]}" apply -k "$ROOT/admin/monitoring"

# 5. 워크로드와 관리자 리소스
"${K[@]}" apply -k "$ROOT"
"${K[@]}" apply -k "$ROOT/admin"

"$ROOT/verify.sh"
