#!/usr/bin/env bash
set -euo pipefail
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
cat >"$dir/all.yaml"
cat >"$dir/kustomization.yaml" <<'YAML'
resources:
  - all.yaml
patches:
  - target:
      kind: StatefulSet
      name: harbor-database
    patch: |-
      - op: add
        path: /spec/template/spec/securityContext/fsGroupChangePolicy
        value: OnRootMismatch
YAML
kubectl kustomize "$dir"
