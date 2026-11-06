"""Read-only Kubernetes projections; do not serialize raw objects or Secrets."""

from __future__ import annotations

from .config import ConfigError


def collect(c, discover=False):
    from kubernetes import client, config

    cfg = client.Configuration()
    config.load_kube_config(
        c["cluster"]["kubeconfig_ref"],
        context=c["cluster"]["context"],
        client_configuration=cfg,
        persist_config=False,
    )
    if not cfg.verify_ssl:
        raise ConfigError("cluster certificate verification required")
    api_client = client.ApiClient(cfg)
    core = client.CoreV1Api(api_client)
    nodes = core.list_node(_request_timeout=20).items
    report = {"nodes": [], "candidates": [], "coverage": [], "reason_codes": []}
    selected = {n["name"]: n for n in c["nodes"]}
    for node in nodes:
        name = node.metadata.name
        ready = any(
            v.type == "Ready" and v.status == "True"
            for v in node.status.conditions or []
        )
        addresses = [v.address for v in node.status.addresses or []]
        if name in selected and (
            selected[name]["uid"] != node.metadata.uid
            or selected[name]["ssh_host"] not in addresses
            or not ready
        ):
            raise ConfigError("cluster node identity or readiness mismatch")
        labels = node.metadata.labels or {}
        report["nodes"].append(
            {
                "name": name,
                "uid": node.metadata.uid,
                "machine_id": node.status.node_info.machine_id,
                "container_runtime_version": getattr(
                    node.status.node_info, "container_runtime_version", ""
                ),
                "ready": ready,
                "control_plane": any(
                    k in labels
                    for k in (
                        "node-role.kubernetes.io/control-plane",
                        "node-role.kubernetes.io/master",
                    )
                ),
            }
        )
    if set(selected) - {n["name"] for n in report["nodes"]}:
        raise ConfigError("selected node missing from cluster")
    if not discover:
        return report
    apps = client.AppsV1Api(api_client)
    networking = client.NetworkingV1Api(api_client)
    namespaces = c["cluster"]["namespaces"] or [None]
    resources = []
    readers = [
        ("pod", core.list_namespaced_pod, core.list_pod_for_all_namespaces),
        ("service", core.list_namespaced_service, core.list_service_for_all_namespaces),
        (
            "deployment",
            apps.list_namespaced_deployment,
            apps.list_deployment_for_all_namespaces,
        ),
        (
            "statefulset",
            apps.list_namespaced_stateful_set,
            apps.list_stateful_set_for_all_namespaces,
        ),
        (
            "replicaset",
            apps.list_namespaced_replica_set,
            apps.list_replica_set_for_all_namespaces,
        ),
        (
            "ingress",
            networking.list_namespaced_ingress,
            networking.list_ingress_for_all_namespaces,
        ),
    ]
    for namespace in namespaces:
        for kind, namespaced, all_ns in readers:
            try:
                values = (
                    namespaced(namespace, _request_timeout=20)
                    if namespace
                    else all_ns(_request_timeout=20)
                ).items
                resources.extend((kind, obj) for obj in values)
                report["coverage"].append(
                    {"resource": kind, "namespace": namespace, "status": "complete"}
                )
            except Exception:
                report["coverage"].append(
                    {"resource": kind, "namespace": namespace, "status": "unavailable"}
                )
                report["reason_codes"].append("KUBERNETES_DISCOVERY_INCOMPLETE")
    by_uid = {obj.metadata.uid: obj for _, obj in resources}
    groups = {}

    def group_key(obj):
        current = obj
        visited = set()
        while current.metadata.uid not in visited:
            visited.add(current.metadata.uid)
            owners = getattr(current.metadata, "owner_references", None) or []
            parent = next((by_uid[o.uid] for o in owners if o.uid in by_uid), None)
            if parent is None:
                break
            current = parent
        labels = obj.metadata.labels or {}
        return (
            obj.metadata.namespace,
            labels.get("app.kubernetes.io/instance") or current.metadata.name,
        )

    for kind, obj in resources:
        labels = obj.metadata.labels or {}
        spec = obj.spec
        containers = getattr(spec, "containers", None)
        if containers is None and getattr(spec, "template", None):
            containers = spec.template.spec.containers
        images = [v.image for v in containers or []]
        if not any(
            "harbor" in str(v).lower()
            for v in [
                obj.metadata.name,
                labels.get("app.kubernetes.io/name", ""),
                *images,
            ]
        ):
            continue
        key = group_key(obj)
        item = groups.setdefault(
            key,
            {
                "kind": "kubernetes",
                "namespace": key[0],
                "name": key[1],
                "components": [],
                "endpoint_candidates": [],
                "product_confirmed": False,
            },
        )
        item["components"].append({"kind": kind, "name": obj.metadata.name})
        if kind == "ingress":
            tls_hosts = {h for t in spec.tls or [] for h in t.hosts or []}
            for rule in spec.rules or []:
                if rule.host:
                    item["endpoint_candidates"].append(
                        {
                            "host": rule.host,
                            "scheme": "https" if rule.host in tls_hosts else None,
                            "port": 443 if rule.host in tls_hosts else None,
                        }
                    )
    report["candidates"] = list(groups.values())
    report["coverage"].append(
        {
            "resource": "host",
            "status": "partial" if len(selected) < len(nodes) else "complete",
        }
    )
    if len(selected) < len(nodes):
        report["reason_codes"].append("HOST_DISCOVERY_INCOMPLETE")
    return report
