"""Allowlisted, secret-free Kubernetes discovery and result aggregation."""

from __future__ import annotations

import hashlib


def candidate_id(*parts):
    return hashlib.sha256("\0".join(str(p) for p in parts).encode()).hexdigest()[:24]


def node_record(node):
    m, s = node.get("metadata", {}), node.get("status", {})
    info = s.get("nodeInfo", {})
    return {
        "name": m.get("name"),
        "uid": m.get("uid"),
        "machine_id": info.get("machineID", ""),
        "system_uuid": info.get("systemUUID", ""),
        "ready": any(
            x.get("type") == "Ready" and x.get("status") == "True"
            for x in s.get("conditions", [])
        ),
        "addresses": [
            {"type": a.get("type"), "address": a.get("address")}
            for a in s.get("addresses", [])
        ],
        "architecture": info.get("architecture"),
        "os_image": info.get("osImage"),
    }


def cluster_report(results, cluster_id):
    """k8s_info loop results may contain credentials: emit only explicit fields."""
    nodes, workloads, pods, services, routes, coverage = [], {}, [], [], [], []
    resources = []
    for r in results:
        item = r.get("item", {})
        scope = {"kind": item.get("kind"), "namespace": item.get("namespace", "*")}
        ok = not r.get("failed") and not r.get("skipped") and r.get("api_found", True)
        coverage.append({**scope, "status": "complete" if ok else "unknown"})
        if ok:
            resources.extend(r.get("resources", []))
    for obj in resources:
        kind, m = obj.get("kind"), obj.get("metadata", {})
        spec = obj.get("spec", {})
        if kind == "Node":
            nodes.append(node_record(obj))
        elif kind in ("Deployment", "StatefulSet", "ReplicaSet"):
            workloads[m.get("uid")] = obj
        elif kind == "Pod":
            pods.append(obj)
        elif kind == "Service":
            services.append(obj)
        elif kind == "Ingress":
            routes.append(obj)
    # Installed but scaled-to-zero workloads still need to appear in the list.
    for uid, workload in workloads.items():
        if workload.get("kind") not in ("Deployment", "StatefulSet"):
            continue
        m = workload.get("metadata", {})
        template = workload.get("spec", {}).get("template", {})
        pods.append(
            {
                "metadata": {
                    "namespace": m.get("namespace"),
                    "name": m.get("name"),
                    "uid": uid,
                    "labels": template.get("metadata", {}).get("labels", {}),
                    "ownerReferences": [{"uid": uid, "controller": True}],
                },
                "spec": template.get("spec", {}),
                "synthetic_workload": True,
            }
        )
    candidates = {}
    for pod in pods:
        m, spec = pod.get("metadata", {}), pod.get("spec", {})
        containers = spec.get("containers", [])
        images = [x.get("image", "") for x in containers]
        labels = m.get("labels", {})
        hints = [str(x).lower() for x in (m.get("name", ""), *images, *labels.values())]
        if not any("keycloak" in h for h in hints):
            continue
        if any("keycloak-operator" in h or "keycloakoperator" in h for h in hints):
            continue
        server_image = any(
            "keycloak" in image.lower().rsplit("/", 1)[-1]
            and not any(
                word in image.lower().rsplit("/", 1)[-1]
                for word in ("postgres", "mysql", "mariadb", "operator")
            )
            for image in images
        )
        companion_image = any(
            any(
                word in image.lower().rsplit("/", 1)[-1]
                for word in ("postgres", "mysql", "mariadb")
            )
            for image in images
        )
        if (
            companion_image
            and not server_image
            and labels.get("app.kubernetes.io/name", "").lower() != "keycloak"
        ):
            continue
        owner_uid = m.get("uid")
        owners = m.get("ownerReferences", [])
        visited = set()
        while owners:
            owner = next((o for o in owners if o.get("controller")), owners[0])
            uid = owner.get("uid")
            if not uid or uid in visited:
                break
            visited.add(uid)
            owner_uid = uid
            owners = (
                workloads.get(uid, {}).get("metadata", {}).get("ownerReferences", [])
            )
        cid = candidate_id(cluster_id, m.get("namespace"), owner_uid)
        c = candidates.setdefault(
            cid,
            {
                "candidate_id": cid,
                "deployment_type": "kubernetes",
                "cluster_id": cluster_id,
                "namespace": m.get("namespace"),
                "resource_uid": owner_uid,
                "nodes": [],
                "pods": [],
                "images": [],
                "endpoints": [],
                "version": None,
                "evidence": ["workload_metadata"],
                "state": "candidate",
                "checks": [],
            },
        )
        if spec.get("nodeName") and spec["nodeName"] not in c["nodes"]:
            c["nodes"].append(spec["nodeName"])
        if not pod.get("synthetic_workload"):
            c["pods"].append(
                {
                    "name": m.get("name"),
                    "uid": m.get("uid"),
                    "phase": pod.get("status", {}).get("phase"),
                }
            )
        c["images"] = sorted(set(c["images"] + images))
        for svc in services:
            sm, ss = svc.get("metadata", {}), svc.get("spec", {})
            selector = ss.get("selector", {})
            if (
                sm.get("namespace") != m.get("namespace")
                or not selector
                or not all(labels.get(k) == v for k, v in selector.items())
            ):
                continue
            for port in ss.get("ports", []):
                endpoint = {
                    "source": "Service",
                    "host": f"{sm.get('name')}.{sm.get('namespace')}.svc",
                    "port": port.get("port"),
                    "scheme": None,
                }
                if endpoint not in c["endpoints"]:
                    c["endpoints"].append(endpoint)
            for route in routes:
                if route.get("metadata", {}).get("namespace") != sm.get("namespace"):
                    continue
                rs = route.get("spec", {})
                for rule in rs.get("rules", []):
                    for path in rule.get("http", {}).get("paths", []):
                        if path.get("backend", {}).get("service", {}).get(
                            "name"
                        ) == sm.get("name"):
                            host = rule.get("host")
                            tls = any(
                                host in t.get("hosts", []) for t in rs.get("tls", [])
                            )
                            endpoint = {
                                "source": "Ingress",
                                "base_url": f"{'https' if tls else 'http'}://{host}{path.get('path', '/')}",
                                "verified": False,
                            }
                            if host and endpoint not in c["endpoints"]:
                                c["endpoints"].append(endpoint)
    return {
        "nodes": nodes,
        "candidates": list(candidates.values()),
        "coverage": coverage,
    }


def aggregate(operation, config, reports, events, returncode):
    cluster = reports.get("cluster", {})
    candidates = list(cluster.get("candidates", []))
    coverage = list(cluster.get("coverage", []))
    checks, reasons = [], []
    for name, r in reports.items():
        if name.startswith("host-node_"):
            node = r.get("node_name")
            coverage.append(
                {
                    "node": node,
                    "scope": "host_services",
                    "status": r.get("coverage", "unknown"),
                    "method": r.get("detection_method"),
                }
            )
            for c in r.get("candidates", []):
                candidates.append(
                    {
                        **c,
                        "candidate_id": candidate_id(
                            config.get("cluster", {}).get("id"), node, c.get("service")
                        ),
                        "nodes": [node],
                        "cluster_id": config.get("cluster", {}).get("id"),
                    }
                )
        checks.extend(r.get("checks", []))
    if operation == "discover":
        configured = {n["name"]: n["uid"] for n in config.get("nodes", [])}
        for node in cluster.get("nodes", []):
            if configured.get(node["name"]) != node["uid"]:
                coverage.append(
                    {
                        "node": node["name"],
                        "scope": "host_services",
                        "status": "unknown",
                    }
                )
                reasons.append("NODE_SSH_INFORMATION_MISSING_OR_STALE")
        reported_nodes = {
            r.get("node_name")
            for name, r in reports.items()
            if name.startswith("host-node_")
        }
        for node in config.get("nodes", []):
            if node["name"] not in reported_nodes:
                coverage.append(
                    {
                        "node": node["name"],
                        "scope": "host_services",
                        "status": "unknown",
                    }
                )
    for event in events:
        reasons.append(event["reason_code"])
    if returncode and not reasons:
        reasons.append("ANSIBLE_EXECUTION_FAILED")
    if operation != "inspect" and not cluster:
        reasons.append("CLUSTER_RESULT_MISSING")
    if operation == "discover" and not cluster.get("nodes"):
        reasons.append("NODE_DISCOVERY_EMPTY_OR_UNAVAILABLE")
    if operation == "inspect" and "endpoint" not in reports:
        reasons.append("ENDPOINT_RESULT_MISSING")
    if operation in ("preflight", "install", "verify") and "target" not in reports:
        reasons.append("TARGET_CHECK_INCOMPLETE")
    if operation in ("install", "verify") and "verified" not in reports:
        reasons.append("INSTALLATION_NOT_VERIFIED")
    partial = any(c.get("status") == "unknown" for c in coverage)
    failed_check = any(c.get("status") == "fail" for c in checks)
    if returncode or reasons or failed_check:
        status = "partial" if operation == "discover" and cluster else "failed"
    else:
        status = "partial" if partial else "complete"
    if failed_check:
        reasons.append("CHECK_FAILED")
        reasons.extend(
            "CHECK_" + c["name"].upper() for c in checks if c.get("status") == "fail"
        )
        if operation == "preflight":
            status = "blocked"
    if partial:
        reasons.append("DISCOVERY_INCOMPLETE")
    return {
        "schema_version": 1,
        "request_id": config["request_id"],
        "operation": operation,
        "status": status,
        "candidates": candidates,
        "coverage": coverage,
        "nodes": cluster.get("nodes", []),
        "checks": checks,
        "planned_changes": reports.get("plan", {}).get("planned_changes", []),
        "reason_codes": sorted(set(reasons)),
        "events": events,
        "connection": reports.get("verified", reports.get("endpoint", {})).get(
            "connection"
        ),
        "next_actions": (
            ["select_candidate_or_enter_existing_endpoint_or_choose_cluster_node"]
            if operation == "discover"
            else []
        ),
        "admin_handoff": "temporary_admin_requires_platform_handoff"
        if operation == "install" and status == "complete"
        else None,
        "scope": "infrastructure_only",
        "limitations": [
            "metadata_and_service_path_detection_cannot_prove_absence_of_custom_installations",
            "application_login_and_iam_migration_not_verified",
        ],
    }
