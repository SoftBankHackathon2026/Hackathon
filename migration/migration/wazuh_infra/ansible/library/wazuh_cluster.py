#!/usr/bin/python
"""Read selected Kubernetes metadata without retrieving Secret objects."""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec={"config": {"type": "dict", "required": True}},
        supports_check_mode=True,
    )
    c = module.params["config"]
    report = {
        "kind": "cluster",
        "nodes": [],
        "candidates": [],
        "checks": [],
        "reason_codes": [],
    }
    if not c.get("cluster"):
        report["checks"].append({"name": "cluster_discovery", "status": "skipped"})
        module.exit_json(changed=False, report=report)
    try:
        from kubernetes import client, config

        config.load_kube_config(
            config_file=c["cluster"]["kubeconfig"],
            context=c["cluster"]["context"],
            persist_config=False,
        )
        core = client.CoreV1Api()
        nodes = core.list_node(_request_timeout=15).items
        for n in nodes:
            report["nodes"].append(
                {
                    "name": n.metadata.name,
                    "uid": n.metadata.uid,
                    "machine_id": n.status.node_info.machine_id,
                    "system_uuid": n.status.node_info.system_uuid,
                    "addresses": [a.address for a in n.status.addresses],
                    "ready": any(
                        x.type == "Ready" and x.status == "True"
                        for x in n.status.conditions
                    ),
                    "control_plane": any(
                        k in n.metadata.labels
                        for k in (
                            "node-role.kubernetes.io/control-plane",
                            "node-role.kubernetes.io/master",
                        )
                    ),
                }
            )
        report["checks"].append({"name": "cluster_nodes", "status": "pass"})
        for ns in c["cluster"].get("namespaces", []) or [None]:
            try:
                pods = (
                    core.list_namespaced_pod(ns, _request_timeout=15).items
                    if ns
                    else core.list_pod_for_all_namespaces(_request_timeout=15).items
                )
                for p in pods:
                    images = [x.image for x in p.spec.containers]
                    if any("wazuh" in x.lower() for x in images):
                        report["candidates"].append(
                            {
                                "deployment_type": "kubernetes",
                                "namespace": p.metadata.namespace,
                                "name": p.metadata.name,
                                "uid": p.metadata.uid,
                                "node_name": p.spec.node_name,
                                "images": images,
                                "evidence": ["pod_image"],
                                "product_confirmed": False,
                            }
                        )
                report["checks"].append(
                    {"name": "pod_discovery:" + (ns or "all"), "status": "pass"}
                )
            except Exception:
                report["checks"].append(
                    {"name": "pod_discovery:" + (ns or "all"), "status": "unknown"}
                )
                report["reason_codes"].append("CLUSTER_WORKLOAD_DISCOVERY_INCOMPLETE")
    except Exception:
        report["checks"].append({"name": "cluster_nodes", "status": "unknown"})
        report["reason_codes"].append("CLUSTER_NODES_UNAVAILABLE")
    module.exit_json(changed=False, report=report)


if __name__ == "__main__":
    main()
