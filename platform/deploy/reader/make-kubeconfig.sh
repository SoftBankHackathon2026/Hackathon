#!/usr/bin/env bash
# reader.yaml 을 적용한 클러스터에서 읽기 전용 kubeconfig 를 만든다. 토큰은 화면에 출력하지 않는다.
#
#   KUBECONFIG=<관리자 kubeconfig> platform/deploy/reader/make-kubeconfig.sh <context 이름> <출력 파일> [API 주소]
#
# API 주소를 생략하면 관리자 kubeconfig 의 주소를 쓴다. 수집기에서 닿는 주소여야 한다.
#   온프렘 k3s : https://<온프렘 마스터 IP>:6443 (Tailscale 경로)
#   GKE        : 공개 엔드포인트. GKE master authorized networks 에 AWS NAT 공인 IP 를 넣는다
set -euo pipefail
context=${1:?context 이름}
out=${2:?출력 파일}
server=${3:-$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')}

umask 077
ca=$(kubectl -n oneport-reader get secret oneport-reader-token -o jsonpath='{.data.ca\.crt}')
token=$(kubectl -n oneport-reader get secret oneport-reader-token -o jsonpath='{.data.token}' | base64 -d)
[[ -n "$ca" && -n "$token" ]] || { echo "토큰 Secret 이 아직 채워지지 않았습니다. reader.yaml 적용 뒤 잠시 기다립니다." >&2; exit 1; }

cat >"$out" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: $context
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: oneport-reader
    user:
      token: $token
contexts:
  - name: $context
    context: {cluster: $context, user: oneport-reader}
current-context: $context
KUBECONFIG
token=
echo "$out 에 $context 읽기 전용 kubeconfig 를 만들었습니다 (권한 600)."
