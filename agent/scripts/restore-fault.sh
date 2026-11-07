#!/usr/bin/env bash
# 데모 장애 원복. 에이전트가 복구하지 못했을 때 쓴다. OpenBao 를 계획서 값으로 되돌리고 재시작한다.
set -euo pipefail
export KUBECONFIG="${KUBECONFIG_ONPREM:-$HOME/.kube/oneport-onprem.yaml}"
kubectl -n hackathon exec deploy/ai-agent -c agent -- python -m agent.demo_fault restore
