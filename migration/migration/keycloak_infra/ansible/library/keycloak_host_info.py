#!/usr/bin/python
"""Read-only native Keycloak and host probe."""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.keycloak_common import host_info, port_free, same_machine


def main():
    module = AnsibleModule(
        argument_spec={
            "node": {"type": "dict", "default": {}},
            "install": {"type": "dict", "default": {}},
            "versions": {"type": "dict", "default": {}},
        },
        supports_check_mode=True,
    )
    info = host_info()
    node, install = module.params["node"], module.params["install"]
    checks = []
    if node:
        checks.append(
            {
                "name": "ssh_node_identity",
                "status": "pass" if same_machine(node, info) else "fail",
            }
        )
    if install:
        versions = module.params["versions"]
        expected = {
            "install_id": install["id"],
            "node_uid": node["uid"],
            "base_url": install["base_url"],
            "https_port": install["https_port"],
            "db_port": install["db_port"],
            **versions,
        }
        manifest = info["managed_manifest"]
        owned = manifest == expected
        supported = info["os_id"] in ("ubuntu", "debian") and info["os_version"] in (
            {"ubuntu": ("24.04", "26.04"), "debian": ("12", "13")}.get(
                info["os_id"], ()
            )
        )
        checks.extend(
            [
                {
                    "name": "service_account_ownership",
                    "status": "pass"
                    if (owned and all(a["uid"] != 0 for a in info["existing_accounts"]))
                    or not info["existing_accounts"]
                    else "fail",
                },
                {
                    "name": "host_discovery",
                    "status": "pass" if info["coverage"] == "complete" else "fail",
                },
                {
                    "name": "supported_os",
                    "status": "pass"
                    if supported
                    and info["systemd"]
                    and info["architecture"] in ("x86_64", "aarch64")
                    else "fail",
                },
                {
                    "name": "available_memory",
                    "status": "pass"
                    if info["memory_available_mb"] >= install["min_memory_mb"]
                    else "fail",
                },
                {
                    "name": "free_disk",
                    "status": "pass"
                    if info["free_disk_mb"] >= install["min_free_disk_mb"]
                    else "fail",
                },
                {
                    "name": "managed_ownership",
                    "status": "pass"
                    if owned
                    or (
                        not manifest
                        and not info["existing_roots"]
                        and not info["unit_conflict"]
                    )
                    else "fail",
                },
                {
                    "name": "existing_keycloak",
                    "status": "pass" if owned or not info["candidates"] else "fail",
                },
            ]
        )
        for port, unit in (
            (install["https_port"], "oneport-keycloak"),
            (7800, "oneport-keycloak"),
            (57800, "oneport-keycloak"),
            (9000, "oneport-keycloak"),
            (install["db_port"], "oneport-keycloak-postgres"),
        ):
            checks.append(
                {
                    "name": "port_" + str(port),
                    "status": "pass"
                    if port_free(port) or (owned and info["managed_active"][unit])
                    else "fail",
                }
            )
        info["expected_manifest"] = expected
    module.exit_json(changed=False, report={**info, "checks": checks})


if __name__ == "__main__":
    main()
