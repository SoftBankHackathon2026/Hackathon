#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/oneport-onprem.yaml}"
SECRETS_FILE="$ROOT/.secrets.env"
TLS_DIR="$ROOT/.local/postgres-tls"
EDGE_TOKEN_FILE="$ROOT/.local/cloudflared-token"

umask 077
# 네임스페이스는 없을 때만 만든다. apply 로 덮으면 kustomize 가 붙인 라벨이 지워진다. # ensure_namespace_note
mkdir -p "$TLS_DIR"

if [[ ! -f "$SECRETS_FILE" ]]; then
  kafka_id="$(uuidgen | tr -d '-' | xxd -r -p | base64 | tr '/+' '_-' | tr -d '=')"
  {
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'MINIO_ROOT_USER=%s\n' "oneaction$(openssl rand -hex 4)"
    printf 'MINIO_ROOT_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'KAFKA_CLUSTER_ID=%s\n' "$kafka_id"
    printf 'GITHUB_WEBHOOK_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'ARGOCD_WEBHOOK_TOKEN=%s\n' "$(openssl rand -hex 32)"
    printf 'REVIEW_API_TOKEN=%s\n' "$(openssl rand -hex 32)"
    printf 'GRAFANA_DB_PASSWORD=%s\n' "$(openssl rand -hex 24)"
  } >"$SECRETS_FILE"
fi
# 이전 버전의 파일에는 Grafana 읽기 전용 DB 비밀번호가 없다. 한 번만 덧붙인다.
if ! grep -q '^GRAFANA_DB_PASSWORD=' "$SECRETS_FILE"; then
  printf 'GRAFANA_DB_PASSWORD=%s\n' "$(openssl rand -hex 24)" >>"$SECRETS_FILE"
fi
if ! grep -q '^ARGOCD_OBSERVER_PASSWORD=' "$SECRETS_FILE"; then
  printf 'ARGOCD_OBSERVER_PASSWORD=%s\n' "$(openssl rand -base64 18 | tr -d '/+=')" >>"$SECRETS_FILE"
fi

set -a
# shellcheck disable=SC1090
source "$SECRETS_FILE"
set +a

# --- Argo CD 로그인 (observer, 읽기 전용). 비밀번호 해시를 Helm 값 파일로 넘긴다. apply.sh 가 helm upgrade 전에
#     이 모드로 먼저 부르고, 나머지 시크릿은 upgrade 뒤에 만든다 (argocd-notifications-secret 소유권 때문).
ARGOCD_LOGIN_VALUES="$ROOT/.local/argocd-login.values.yaml"
if [[ ! -f "$ARGOCD_LOGIN_VALUES" ]] || \
   ! htpasswd -vbB "$ARGOCD_LOGIN_VALUES.htpasswd" observer "$ARGOCD_OBSERVER_PASSWORD" >/dev/null 2>&1; then
  htpasswd -cbBC 10 "$ARGOCD_LOGIN_VALUES.htpasswd" observer "$ARGOCD_OBSERVER_PASSWORD" >/dev/null 2>&1
  hash="$(cut -d: -f2 "$ARGOCD_LOGIN_VALUES.htpasswd" | sed 's/^\$2y\$/$2a$/')"
  cat >"$ARGOCD_LOGIN_VALUES" <<YAML
configs:
  secret:
    extra:
      accounts.observer.password: "$hash"
      accounts.observer.passwordMtime: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
YAML
fi
if [[ "${1:-}" == "argocd-login" ]]; then
  exit 0
fi

if [[ ! -f "$TLS_DIR/tls.key" || ! -f "$TLS_DIR/tls.crt" ]]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -subj '/CN=postgres.hackathon.svc.cluster.local' \
    -addext 'subjectAltName=DNS:postgres,DNS:postgres.hackathon,DNS:postgres.hackathon.svc,DNS:postgres.hackathon.svc.cluster.local' \
    -keyout "$TLS_DIR/tls.key" -out "$TLS_DIR/tls.crt" >/dev/null 2>&1
fi

kubectl --kubeconfig "$KUBECONFIG_PATH" get namespace hackathon >/dev/null 2>&1 || \
  kubectl --kubeconfig "$KUBECONFIG_PATH" create namespace hackathon >/dev/null

kubectl --kubeconfig "$KUBECONFIG_PATH" -n hackathon create secret generic review-db-credentials \
  --from-literal=username=oneaction_admin \
  --from-literal=password="$POSTGRES_PASSWORD" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -

kubectl --kubeconfig "$KUBECONFIG_PATH" -n hackathon create secret generic hackathon-platform-secrets \
  --from-literal=MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  --from-literal=MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
  --from-literal=KAFKA_CLUSTER_ID="$KAFKA_CLUSTER_ID" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -

kubectl --kubeconfig "$KUBECONFIG_PATH" -n hackathon create secret generic review-service-secrets \
  --from-literal=GITHUB_WEBHOOK_SECRET="$GITHUB_WEBHOOK_SECRET" \
  --from-literal=ARGOCD_WEBHOOK_TOKEN="$ARGOCD_WEBHOOK_TOKEN" \
  --from-literal=REVIEW_API_TOKEN="$REVIEW_API_TOKEN" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -

kubectl --kubeconfig "$KUBECONFIG_PATH" -n hackathon create secret tls postgres-tls \
  --cert="$TLS_DIR/tls.crt" --key="$TLS_DIR/tls.key" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -

if [[ ! -s "$EDGE_TOKEN_FILE" ]] && \
  kubectl --kubeconfig "$KUBECONFIG_PATH" -n oneport-system get secret cloudflared-token >/dev/null 2>&1; then
  kubectl --kubeconfig "$KUBECONFIG_PATH" -n oneport-system get secret cloudflared-token \
    -o jsonpath='{.data.token}' | openssl base64 -d -A >"$EDGE_TOKEN_FILE"
fi

if [[ -s "$EDGE_TOKEN_FILE" ]]; then
  kubectl --kubeconfig "$KUBECONFIG_PATH" get namespace edge-system >/dev/null 2>&1 || \
    kubectl --kubeconfig "$KUBECONFIG_PATH" create namespace edge-system >/dev/null
  kubectl --kubeconfig "$KUBECONFIG_PATH" -n edge-system create secret generic cloudflared-token \
    --from-file=token="$EDGE_TOKEN_FILE" --dry-run=client -o yaml | \
    kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -
fi

# --- AWS 대응: Argo CD Notifications 토큰 (AWS 는 ESO 가 Secrets Manager 에서 argocd-notifications-secret 에 넣는다)
kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd create secret generic argocd-notifications-secret \
  --from-literal=ARGOCD_WEBHOOK_TOKEN="$ARGOCD_WEBHOOK_TOKEN" \
  --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -

# --- Argo CD UI 인증서: Route 53 DNS-01 용 IAM 키 (yubin.dev 존 레코드 변경만 허용된 키)
kubectl --kubeconfig "$KUBECONFIG_PATH" get namespace cert-manager >/dev/null 2>&1 || \
  kubectl --kubeconfig "$KUBECONFIG_PATH" create namespace cert-manager >/dev/null
if [[ -n "${ROUTE53_ACCESS_KEY_ID:-}" && -n "${ROUTE53_SECRET_ACCESS_KEY:-}" ]]; then
  kubectl --kubeconfig "$KUBECONFIG_PATH" -n cert-manager create secret generic route53-credentials \
    --from-literal=AWS_ACCESS_KEY_ID="$ROUTE53_ACCESS_KEY_ID" \
    --from-literal=AWS_SECRET_ACCESS_KEY="$ROUTE53_SECRET_ACCESS_KEY" \
    --dry-run=client -o yaml | kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -
else
  echo "WARN: ROUTE53_ACCESS_KEY_ID / ROUTE53_SECRET_ACCESS_KEY not in .secrets.env; argo.yubin.dev certificate stays pending" >&2
fi

# --- AWS 대응: 모니터링 (gitops argocd/monitoring/README.md 의 grafana-admin, review-db 데이터소스)
kubectl --kubeconfig "$KUBECONFIG_PATH" get namespace monitoring >/dev/null 2>&1 || \
  kubectl --kubeconfig "$KUBECONFIG_PATH" create namespace monitoring >/dev/null
if ! kubectl --kubeconfig "$KUBECONFIG_PATH" -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  kubectl --kubeconfig "$KUBECONFIG_PATH" -n monitoring create secret generic grafana-admin \
    --from-literal=admin-user=admin \
    --from-literal=admin-password="$(openssl rand -base64 24 | tr -d '/+=')"
fi
datasource="$(cat <<YAML
apiVersion: 1
datasources:
  - name: review-db
    uid: review-db
    type: grafana-postgresql-datasource
    url: postgres.hackathon.svc.cluster.local:5432
    user: grafana_ro
    editable: false
    jsonData:
      database: oneaction_review
      sslmode: require
      postgresVersion: 1600
      maxOpenConns: 5
    secureJsonData:
      password: "$GRAFANA_DB_PASSWORD"
YAML
)"
kubectl --kubeconfig "$KUBECONFIG_PATH" -n monitoring create secret generic review-db-grafana-datasource \
  --from-literal=review-db.yaml="$datasource" --dry-run=client -o yaml | \
  kubectl --kubeconfig "$KUBECONFIG_PATH" label --local -f - grafana_datasource=1 -o yaml | \
  kubectl --kubeconfig "$KUBECONFIG_PATH" apply -f -

# Grafana 읽기 전용 DB 사용자 (gitops apps/review-service/monitoring/README.md 1번). PostgreSQL 이 떠 있을 때만.
if kubectl --kubeconfig "$KUBECONFIG_PATH" -n hackathon get pod postgres-0 >/dev/null 2>&1; then
  kubectl --kubeconfig "$KUBECONFIG_PATH" -n hackathon exec -i postgres-0 -- \
    env GRAFANA_DB_PASSWORD="$GRAFANA_DB_PASSWORD" sh -ec '
      psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '"'"'grafana_ro'"'"') THEN
    CREATE ROLE grafana_ro LOGIN;
  END IF;
END
\$\$;
ALTER ROLE grafana_ro WITH PASSWORD '"'"'$GRAFANA_DB_PASSWORD'"'"';
GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
SQL' >/dev/null
fi

echo "Secrets reconciled in namespaces hackathon, argocd, monitoring."

