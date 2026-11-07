#!/usr/bin/python
"""Execute a fixed host action; never return subprocess output or exception text."""

from ansible.module_utils import harbor_host as host
from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.harbor_core import HarborError


def main():
    m = AnsibleModule(
        argument_spec={
            "operation": {
                "type": "str",
                "required": True,
                "choices": [
                    "discover",
                    "preflight",
                    "install",
                    "trust_preflight",
                    "trust",
                    "verify",
                ],
            },
            "config": {"type": "dict", "required": True},
            "node": {"type": "dict", "required": True},
            "secrets_value": {
                "type": "dict",
                "default": {},
                "options": {
                    key: {
                        "type": "str",
                        "no_log": key
                        in (
                            "admin_password",
                            "database_password",
                            "oidc_client_secret",
                            "keycloak_admin_token",
                            "tls_private_key",
                        ),
                    }
                    for key in (
                        "endpoint_ca",
                        "admin_password",
                        "database_password",
                        "oidc_client_secret",
                        "keycloak_admin_token",
                        "oidc_ca",
                        "tls_certificate",
                        "tls_ca",
                        "tls_private_key",
                    )
                },
            },
            "releases": {"type": "dict", "default": {}},
            "harbor_yaml": {"type": "str", "default": "", "no_log": True},
        },
        supports_check_mode=True,
    )
    op, c, node = m.params["operation"], m.params["config"], m.params["node"]
    try:
        if m.check_mode and op in ("install", "trust"):
            raise HarborError("USE_PREFLIGHT_FOR_CHANGE_PLAN")
        if op == "discover":
            host.identity(c, node)
            report = host.discover_host(node)
        elif op == "preflight":
            report = host.preflight(c, node, m.params["releases"])
        elif op == "install":
            report = host.install(
                c,
                node,
                m.params["secrets_value"],
                m.params["releases"],
                m.params["harbor_yaml"],
            )
        elif op in ("trust", "trust_preflight"):
            report = host.trust(
                c, node, m.params["secrets_value"]["endpoint_ca"], op == "trust"
            )
        else:
            report = host.verify_host(c, node)
        m.exit_json(changed=bool(report.pop("changed", False)), report=report)
    except HarborError as exc:
        m.fail_json(msg="Harbor host operation blocked", reason_code=exc.code)
    except Exception:
        m.fail_json(
            msg="Harbor host operation failed",
            reason_code="HOST_OPERATION_FAILED_REMOTE_STATE_UNKNOWN",
        )


if __name__ == "__main__":
    main()
