"""Explicit opt-in integration checks; no secret reads or cluster mutations."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from controlplane.domain.workload_inventory import analyze


@pytest.mark.parametrize("overlay", ["onprem", "aws"])
def test_rendered_gitops_sample_app(overlay):
    root = os.getenv("INVENTORY_TEST_GITOPS_ROOT")
    if not root:
        pytest.skip("set INVENTORY_TEST_GITOPS_ROOT to a reviewed gitops checkout")
    process = subprocess.run(
        ["kubectl", "kustomize", str(Path(root) / "apps/sample-app/overlays" / overlay)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, "kustomize failed (raw output withheld)"
    result = analyze(
        {"kind": "List", "items": list(yaml.safe_load_all(process.stdout))}, "hackathon"
    )
    assert any(r["kind"] == "Rollout" and r["name"] == "sample-app" for r in result["resources"])
    assert result["images"] and result["dependencies"]
    assert result["readiness"] == "not_evaluated"


def test_live_onprem_api_to_analyzer():
    kubeconfig = os.getenv("INVENTORY_TEST_KUBECONFIG")
    context = os.getenv("INVENTORY_TEST_CONTEXT")
    namespace = os.getenv("INVENTORY_TEST_NAMESPACE")
    if not all([kubeconfig, context, namespace]):
        pytest.skip("explicit INVENTORY_TEST_KUBECONFIG/CONTEXT/NAMESPACE required")
    resources = "deployments,statefulsets,services,ingresses,persistentvolumeclaims,serviceaccounts,rollouts.argoproj.io"
    process = subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "--context",
            context,
            "--request-timeout=15s",
            "-n",
            namespace,
            "get",
            resources,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 0, "cluster resource read failed (raw output withheld)"
    result = analyze(json.loads(process.stdout), namespace)
    assert result["images"]
    assert any(r["kind"] == "Rollout" and r["name"] == "sample-app" for r in result["resources"])
    assert any(
        d["source"]["name"] == "sample-app" and d["target"]["kind"] == "Secret"
        for d in result["dependencies"]
    )
    assert all(r["kind"] != "Secret" for r in result["resources"])
    assert result["readiness"] == "not_evaluated"
