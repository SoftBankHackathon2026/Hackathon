#!/usr/bin/env bash
# 데모 장애 주입. 공용 온프렘 sample-app 이 내려간다. 시연 시간을 팀과 맞춘 뒤 실행한다.
# 현재 컨텍스트가 EKS 일 수 있어 온프렘 kubeconfig 를 명시한다.
set -euo pipefail
export KUBECONFIG="${KUBECONFIG_ONPREM:-$HOME/.kube/oneport-onprem.yaml}"
kubectl -n hackathon exec deploy/ai-agent -c agent -- python -m agent.demo_fault inject
