#!/usr/bin/python
"""Project Kubernetes reads into minimal reports, never expose workload env/Secrets."""

from ansible.module_utils.basic import AnsibleModule


def main():
    m = AnsibleModule(
        argument_spec={
            "config": {"type": "dict", "required": True},
            "discover": {"type": "bool", "default": False},
        },
        supports_check_mode=True,
    )
    try:
        from kubernetes import client, config

        c = m.params["config"]
        config.load_kube_config(
            config_file=c["cluster"]["kubeconfig"], context=c["cluster"]["context"]
        )
        api = client.CoreV1Api()
        nodes = api.list_node(_request_timeout=20).items
        selected = {n["name"]: n for n in c["nodes"]}
        reports = []
        for node in nodes:
            addresses = [a.address for a in node.status.addresses or []]
            n = selected.get(node.metadata.name)
            if n and (node.metadata.uid != n["uid"] or n["ssh_host"] not in addresses):
                m.fail_json(
                    msg="Node identity mismatch", reason_code="NODE_IDENTITY_MISMATCH"
                )
            labels = node.metadata.labels or {}
            reports.append(
                {
                    "name": node.metadata.name,
                    "uid": node.metadata.uid,
                    "machine_id": node.status.node_info.machine_id,
                    "ready": any(
                        x.type == "Ready" and x.status == "True"
                        for x in node.status.conditions or []
                    ),
                    "control_plane": any(
                        x in labels
                        for x in (
                            "node-role.kubernetes.io/control-plane",
                            "node-role.kubernetes.io/master",
                        )
                    ),
                }
            )
        if set(selected) - {n["name"] for n in reports}:
            m.fail_json(msg="Node not found", reason_code="NODE_NOT_IN_CLUSTER")
        candidates = []
        incomplete = []
        if m.params["discover"]:
            try:
                pods = api.list_pod_for_all_namespaces(_request_timeout=30).items
                for pod in pods:
                    for container in pod.spec.containers or []:
                        if "openbao" in container.image.lower():
                            candidates.append(
                                {
                                    "kind": "kubernetes_pod",
                                    "namespace": pod.metadata.namespace,
                                    "name": pod.metadata.name,
                                    "node_name": pod.spec.node_name,
                                    "product_confirmed": False,
                                }
                            )
            except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
                incomplete.append("POD_DISCOVERY_INCOMPLETE")
            try:
                for svc in api.list_service_for_all_namespaces(
                    _request_timeout=30
                ).items:
                    if (
                        "openbao" in svc.metadata.name.lower()
                        or "openbao" in str(svc.spec.selector or {}).lower()
                    ):
                        candidates.append(
                            {
                                "kind": "kubernetes_service",
                                "namespace": svc.metadata.namespace,
                                "name": svc.metadata.name,
                                "product_confirmed": False,
                                "ports": [p.port for p in svc.spec.ports or []],
                            }
                        )
            except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
                incomplete.append("SERVICE_DISCOVERY_INCOMPLETE")
        m.exit_json(
            changed=False,
            report={
                "status": "discovered",
                "nodes": reports,
                "candidates": candidates,
                "reason_codes": incomplete,
            },
        )
    except ImportError:
        m.fail_json(
            msg="Kubernetes client required", reason_code="KUBERNETES_CLIENT_REQUIRED"
        )
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        m.fail_json(msg="Cluster read failed", reason_code="CLUSTER_READ_FAILED")


if __name__ == "__main__":
    main()
