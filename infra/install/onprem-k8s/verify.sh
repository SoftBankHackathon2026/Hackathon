#!/usr/bin/env bash
set -euo pipefail

KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/oneport-onprem.yaml}"
K=(kubectl --kubeconfig "$KUBECONFIG_PATH" -n hackathon)

"${K[@]}" wait --for=condition=Ready pod -l app=postgres --timeout=300s
"${K[@]}" wait --for=condition=Ready pod -l app=kafka-controller --timeout=420s
"${K[@]}" wait --for=condition=Ready pod -l app=kafka-broker --timeout=420s
"${K[@]}" wait --for=condition=Ready pod -l app=object-storage --timeout=300s
"${K[@]}" rollout status deployment/review-api --timeout=300s
"${K[@]}" rollout status deployment/review-worker --timeout=300s

"${K[@]}" exec statefulset/postgres -- sh -ec \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql "sslmode=require host=127.0.0.1 user=$POSTGRES_USER dbname=$POSTGRES_DB" -Atc "select version();"' >/dev/null

"${K[@]}" exec kafka-broker-0 -- /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 --create --if-not-exists \
  --topic oneaction.verify --partitions 3 --replication-factor 2 >/dev/null
"${K[@]}" exec kafka-broker-0 -- /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server kafka:9092 --describe --topic oneaction.verify

"${K[@]}" wait --for=condition=complete job/object-storage-bootstrap --timeout=300s
"${K[@]}" rollout status deployment/sample-app-loadgen --timeout=120s

# AWS 대응 기능: 알림 컨트롤러, Prometheus 수집, 비SSL DB 접속 거부
kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd rollout status deployment/argocd-notifications-controller --timeout=120s
kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd rollout status deployment/argocd-server --timeout=120s
# Argo CD UI: Traefik websecure 를 통해 (DNS 없이도 LAN 에서 확인 가능; 인증서 발급 전에는 Traefik 기본 인증서)
if [[ "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 --resolve argo.yubin.dev:443:172.30.1.10 https://argo.yubin.dev/healthz)" != "200" ]]; then
  echo "ERROR: Argo CD is not reachable through Traefik as https://argo.yubin.dev" >&2; exit 1
fi
kubectl --kubeconfig "$KUBECONFIG_PATH" -n cert-manager rollout status deployment/cert-manager --timeout=120s
if kubectl --kubeconfig "$KUBECONFIG_PATH" -n cert-manager get secret route53-credentials >/dev/null 2>&1; then
  kubectl --kubeconfig "$KUBECONFIG_PATH" -n argocd wait --for=condition=Ready certificate/argocd-server-tls --timeout=300s
  issuer="$(echo | openssl s_client -connect 172.30.1.10:443 -servername argo.yubin.dev 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null)"
  case "$issuer" in *"Let's Encrypt"*) ;; *) echo "ERROR: argo.yubin.dev is not serving a Let's Encrypt certificate: $issuer" >&2; exit 1;; esac
else
  echo "WARN: cert-manager/route53-credentials missing; argo.yubin.dev certificate not verified" >&2
fi
kubectl --kubeconfig "$KUBECONFIG_PATH" -n monitoring rollout status statefulset/prometheus-monitoring-kube-prometheus-prometheus --timeout=300s
if "${K[@]}" exec postgres-0 -- sh -ec \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql "sslmode=disable host=127.0.0.1 user=$POSTGRES_USER dbname=$POSTGRES_DB" -Atc "select 1"' >/dev/null 2>&1; then
  echo "ERROR: PostgreSQL accepted a non-TLS connection" >&2; exit 1
fi

"${K[@]}" get pods,pvc,ingress

