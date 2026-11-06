"""Read-only inventory of Kubernetes JSON exports; never return Secret/config values."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

WORKLOADS = {
    "Pod",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "ReplicaSet",
    "Rollout",
}
SUPPORTED = WORKLOADS | {
    "Service",
    "Ingress",
    "Secret",
    "ConfigMap",
    "PersistentVolumeClaim",
    "ServiceAccount",
    "NetworkPolicy",
}


def analyze(document: Any, namespace: str = "default") -> dict[str, Any]:
    """Return references and prerequisites, not manifests or a readiness verdict.

    Missing references mean 'not present in this export', not 'absent in cluster'.
    Cluster access, secret reads, target provisioning and writes are intentionally absent.
    """
    if not isinstance(document, dict):
        raise ValueError("expected a Kubernetes object or List")
    items = document.get("items") if document.get("kind", "").endswith("List") else [document]
    if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
        raise ValueError("expected object items")
    resources, refs, images, requirements = [], set(), set(), set()
    present = set()
    for obj in items:
        kind = obj.get("kind", "Unknown")
        meta = obj.get("metadata") or {}
        name, ns = meta.get("name"), meta.get("namespace", namespace)
        if not isinstance(name, str) or not isinstance(ns, str):
            raise ValueError("resource name and namespace required")
        present.add((kind, ns, name))
        row = {"kind": kind, "namespace": ns, "name": name, "supported": kind in SUPPORTED}
        resources.append(row)
        spec = obj.get("spec") or {}

        def ref(target_kind: str, target: Any) -> None:
            if isinstance(target, str) and target:
                refs.add((kind, ns, name, target_kind, target))

        if kind in WORKLOADS:
            if kind == "Rollout":
                requirements.add(
                    "Argo Rollouts controller, analysis templates and traffic provider need target review"
                )
            if kind == "CronJob":
                pod = (
                    spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
                )
            elif kind == "Pod":
                pod = spec
            else:
                pod = spec.get("template", {}).get("spec", {})
            ref("ServiceAccount", pod.get("serviceAccountName", "default"))
            for pull in pod.get("imagePullSecrets", []):
                ref("Secret", pull.get("name"))
            for container in pod.get("containers", []) + pod.get("initContainers", []):
                if isinstance(container.get("image"), str):
                    images.add(container["image"])
                for env in container.get("env", []):
                    source = env.get("valueFrom") or {}
                    ref("Secret", source.get("secretKeyRef", {}).get("name"))
                    ref("ConfigMap", source.get("configMapKeyRef", {}).get("name"))
                for source in container.get("envFrom", []):
                    ref("Secret", source.get("secretRef", {}).get("name"))
                    ref("ConfigMap", source.get("configMapRef", {}).get("name"))
            for volume in pod.get("volumes", []):
                ref("Secret", volume.get("secret", {}).get("secretName"))
                ref("ConfigMap", volume.get("configMap", {}).get("name"))
                ref(
                    "PersistentVolumeClaim",
                    volume.get("persistentVolumeClaim", {}).get("claimName"),
                )
                for source in volume.get("projected", {}).get("sources", []):
                    ref("Secret", source.get("secret", {}).get("name"))
                    ref("ConfigMap", source.get("configMap", {}).get("name"))
                if "hostPath" in volume:
                    requirements.add("hostPath data/placement needs manual migration review")
                if "emptyDir" in volume:
                    requirements.add("emptyDir data is ephemeral; confirm app tolerates loss")
                if "csi" in volume:
                    requirements.add("CSI driver and volume provider need target review")
            if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
                requirements.add("host namespace access needs target security review")
            if pod.get("nodeSelector") or pod.get("affinity") or pod.get("tolerations"):
                requirements.add("scheduling constraints need target node mapping")
            if kind == "StatefulSet" and spec.get("volumeClaimTemplates"):
                requirements.add(
                    "StatefulSet claim templates require storage mapping and data migration"
                )
        elif kind == "Service":
            row["service_type"] = spec.get("type", "ClusterIP")
            if row["service_type"] in {"LoadBalancer", "NodePort", "ExternalName"}:
                requirements.add("service exposure requires target LB/DNS/routes/firewall review")
        elif kind == "Ingress":
            requirements.add(
                "Ingress controller/class, TLS, DNS and fixed endpoint mapping required"
            )
            for tls in spec.get("tls", []):
                ref("Secret", tls.get("secretName"))
            backends = [spec.get("defaultBackend", {})]
            for rule in spec.get("rules", []):
                backends += [p.get("backend", {}) for p in rule.get("http", {}).get("paths", [])]
            for backend in backends:
                ref("Service", backend.get("service", {}).get("name"))
        elif kind == "PersistentVolumeClaim":
            requirements.add(
                "PVC storage class/access modes/capacity and data migration need target review"
            )
        elif kind == "NetworkPolicy":
            requirements.add(
                "NetworkPolicy enforcement and external VPC/firewall rules need target review"
            )
        elif kind == "ServiceAccount":
            requirements.add("service account cloud identity binding needs target mapping")
        if kind not in SUPPORTED:
            requirements.add("unsupported resources require manual review")
    dependencies = [
        {
            "source": {"kind": k, "namespace": ns, "name": n},
            "target": {"kind": tk, "namespace": ns, "name": tn},
            "present_in_export": (tk, ns, tn) in present,
        }
        for k, ns, n, tk, tn in sorted(refs)
    ]
    return {
        "schema_version": 1,
        "resources": sorted(resources, key=lambda r: (r["namespace"], r["kind"], r["name"])),
        "images": sorted(images),
        "dependencies": dependencies,
        "target_review_required": sorted(requirements),
        "readiness": "not_evaluated",
        "secret_values_included": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--namespace", default="default", help="fallback for objects without namespace"
    )
    args = parser.parse_args()
    try:
        result = analyze(json.load(sys.stdin), args.namespace)
    except (ValueError, TypeError, AttributeError, KeyError):
        # Never echo input/error text: a malformed input may contain credentials.
        print("invalid Kubernetes JSON export", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
