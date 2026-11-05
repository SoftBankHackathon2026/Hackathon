#!/bin/sh
# Oneport 환경 연결 — 온프렘 Kubernetes
#
# kubectl 이 되는 셸(클러스터 관리자 권한)에서 실행합니다. 이 스크립트가 하는 일:
#   1. 클러스터에 읽기 전용 계정을 만듭니다 (네임스페이스 oneport-reader, ServiceAccount, ClusterRole oneport-fleet-reader).
#      Secret 은 읽지 않습니다. 권한: namespaces·pods·resourcequotas·deployments·statefulsets·networkpolicies·roles 조회,
#      argocd-cm ConfigMap, Argo CD Application, ExternalSecret 목록.
#   2. 그 계정의 토큰으로 kubeconfig 를 만듭니다. 이 kubeconfig 만 플랫폼에 보냅니다. 지금 쓰는 관리자 kubeconfig 는 보내지 않습니다.
#   3. 노드(이름·역할·CPU·메모리·OS·런타임), 버전, StorageClass, IngressClass, 네임스페이스 수, 설치된 컨트롤러 이름을 읽습니다.
#   4. 결과를 플랫폼의 등록 주소로 보냅니다. 등록 토큰은 한 번만 쓸 수 있고 1시간 뒤 만료됩니다.
#
# 사용법:
#   sh oneport-enroll-k8s.sh --platform <플랫폼 주소> --enrollment <등록 ID> --token <등록 토큰> [--server <API 주소>] [--context <context>]
#   --server : 플랫폼(수집기)에서 닿는 kube API 주소. 생략하면 현재 kubeconfig 의 주소를 씁니다.
set -eu

PLATFORM= ENROLLMENT= TOKEN= SERVER= CONTEXT=
while [ $# -gt 0 ]; do
  case "$1" in
    --platform) PLATFORM=$2; shift 2 ;;
    --enrollment) ENROLLMENT=$2; shift 2 ;;
    --token) TOKEN=$2; shift 2 ;;
    --server) SERVER=$2; shift 2 ;;
    --context) CONTEXT=$2; shift 2 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
done
[ -n "$PLATFORM" ] && [ -n "$ENROLLMENT" ] && [ -n "$TOKEN" ] || { echo "--platform, --enrollment, --token 이 필요합니다." >&2; exit 2; }
for tool in kubectl curl sed awk; do command -v "$tool" >/dev/null 2>&1 || { echo "필요한 명령이 없습니다: $tool" >&2; exit 1; }; done

KC="kubectl"
[ -n "$CONTEXT" ] && KC="kubectl --context $CONTEXT"
CONTEXT=${CONTEXT:-$(kubectl config current-context)}
SERVER=${SERVER:-$($KC config view --minify -o jsonpath='{.clusters[0].cluster.server}')}
case "$SERVER" in https://*) ;; *) echo "kube API 주소는 https:// 로 시작해야 합니다: $SERVER" >&2; exit 1 ;; esac

echo "[1/4] 클러스터 확인: context $CONTEXT, API $SERVER"
$KC version -o json >/dev/null
if [ "$($KC auth can-i create clusterrolebindings 2>/dev/null)" != "yes" ]; then
  echo "읽기 계정을 만들 권한(clusterrolebindings 생성)이 없습니다. 클러스터 관리자 kubeconfig 로 실행하세요." >&2
  exit 1
fi

echo "[2/4] 읽기 전용 계정 만들기 (oneport-reader)"
$KC apply -f - >/dev/null <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  name: oneport-reader
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: oneport-reader
  namespace: oneport-reader
---
apiVersion: v1
kind: Secret
metadata:
  name: oneport-reader-token
  namespace: oneport-reader
  annotations:
    kubernetes.io/service-account.name: oneport-reader
type: kubernetes.io/service-account-token
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: oneport-fleet-reader
rules:
  - apiGroups: [""]
    resources: [namespaces, pods, resourcequotas]
    verbs: [get, list]
  - apiGroups: [""]
    resources: [configmaps]
    resourceNames: [argocd-cm]
    verbs: [get]
  - apiGroups: [apps]
    resources: [deployments, statefulsets]
    verbs: [get, list]
  - apiGroups: [networking.k8s.io]
    resources: [networkpolicies]
    verbs: [list]
  - apiGroups: [rbac.authorization.k8s.io]
    resources: [roles]
    verbs: [list]
  - apiGroups: [argoproj.io]
    resources: [applications]
    verbs: [get, list]
  - apiGroups: [external-secrets.io]
    resources: [externalsecrets]
    verbs: [list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: oneport-fleet-reader
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: oneport-fleet-reader
subjects:
  - kind: ServiceAccount
    name: oneport-reader
    namespace: oneport-reader
YAML

i=0
until [ -n "$($KC -n oneport-reader get secret oneport-reader-token -o jsonpath='{.data.token}' 2>/dev/null)" ]; do
  i=$((i + 1)); [ "$i" -gt 30 ] && { echo "읽기 계정 토큰이 30초 안에 만들어지지 않았습니다." >&2; exit 1; }
  sleep 1
done

umask 077
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM

CA=$($KC -n oneport-reader get secret oneport-reader-token -o jsonpath='{.data.ca\.crt}')
READER_TOKEN=$($KC -n oneport-reader get secret oneport-reader-token -o jsonpath='{.data.token}' | base64 -d)
cat >"$WORK/kubeconfig" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: $CONTEXT
    cluster:
      server: $SERVER
      certificate-authority-data: $CA
users:
  - name: oneport-reader
    user:
      token: $READER_TOKEN
contexts:
  - name: $CONTEXT
    context: {cluster: $CONTEXT, user: oneport-reader}
current-context: $CONTEXT
KUBECONFIG
READER_TOKEN=

echo "[3/4] 환경 정보 읽기"
$KC version -o json >"$WORK/version"
$KC get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.capacity.cpu}{"|"}{.status.capacity.memory}{"|"}{.status.allocatable.cpu}{"|"}{.status.allocatable.memory}{"|"}{.status.nodeInfo.kubeletVersion}{"|"}{.status.nodeInfo.osImage}{"|"}{.status.nodeInfo.kernelVersion}{"|"}{.status.nodeInfo.containerRuntimeVersion}{"|"}{.status.nodeInfo.architecture}{"|"}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}' >"$WORK/nodes"
{ $KC get nodes -l node-role.kubernetes.io/control-plane -o name; $KC get nodes -l node-role.kubernetes.io/master -o name; } 2>/dev/null | sort -u >"$WORK/control"
$KC get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.provisioner}{"|"}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' >"$WORK/storage" 2>/dev/null || : >"$WORK/storage"
$KC get ingressclass -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' >"$WORK/ingress" 2>/dev/null || : >"$WORK/ingress"
$KC get deployments -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{"\n"}{end}' >"$WORK/deployments"
NAMESPACES=$($KC get namespaces -o name | wc -l | tr -d ' ')
echo "      노드 $(wc -l <"$WORK/nodes" | tr -d ' ')대, 네임스페이스 ${NAMESPACES}개"

# JSON 문자열로 바꾼다 (백슬래시·따옴표 이스케이프, 줄바꿈은 \n). jq 없이 POSIX 도구만 쓴다.
json_file() { printf '"'; tr -d '\r' <"$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | awk 'BEGIN{ORS=""} {if (NR>1) printf "%s", "\\n"; printf "%s", $0}'; printf '"'; }
json_text() { printf '%s' "$1" >"$WORK/text"; json_file "$WORK/text"; }

{
  printf '{"kind":"kubernetes","context":'; json_text "$CONTEXT"
  printf ',"server":'; json_text "$SERVER"
  printf ',"version_json":'; json_file "$WORK/version"
  printf ',"nodes_tsv":'; json_file "$WORK/nodes"
  printf ',"control_plane_nodes":'; json_file "$WORK/control"
  printf ',"storage_tsv":'; json_file "$WORK/storage"
  printf ',"ingress_classes":'; json_file "$WORK/ingress"
  printf ',"namespace_count":%s' "$NAMESPACES"
  printf ',"deployments":'; json_file "$WORK/deployments"
  printf ',"kubeconfig":'; json_file "$WORK/kubeconfig"
  printf '}'
} >"$WORK/payload.json"

echo "[4/4] 플랫폼에 등록"
status=$(curl -sS -o "$WORK/response" -w '%{http_code}' -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @"$WORK/payload.json" "${PLATFORM%/}/api/v1/enrollments/$ENROLLMENT/complete")
if [ "$status" != "200" ]; then
  echo "등록하지 못했습니다 (HTTP $status): $(cat "$WORK/response")" >&2
  exit 1
fi
echo "등록을 마쳤습니다. 플랫폼 화면에서 환경 정보를 확인하세요."
