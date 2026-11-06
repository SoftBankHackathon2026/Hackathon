#!/usr/bin/python
"""Controller-side API calls; administrator credentials never cross to discovery."""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.harbor_core import (
    Api,
    HarborError,
    check_oidc,
    configure,
    ensure_keycloak_client,
    inspect,
)


def main():
    m = AnsibleModule(
        argument_spec={
            "operation": {
                "type": "str",
                "choices": ["inspect", "configure"],
                "required": True,
            },
            "config": {"type": "dict", "required": True},
            "secrets_value": {
                "type": "dict",
                "required": True,
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
            "state_directory": {"type": "str", "default": ""},
        },
        supports_check_mode=True,
    )
    try:
        c, secrets_value = m.params["config"], m.params["secrets_value"]
        ep = c["endpoint"]
        api = Api(
            ep["url"],
            secrets_value.get("endpoint_ca", ""),
            ep.get("admin_username", "") if secrets_value.get("admin_password") else "",
            secrets_value.get("admin_password", ""),
        )
        if m.params["operation"] == "inspect":
            report = inspect(api)
        else:
            if m.check_mode:
                raise HarborError("USE_PREFLIGHT_FOR_CHANGE_PLAN")
            inspection = inspect(api)
            if not inspection["product_confirmed"]:
                raise HarborError("PRODUCT_UNCONFIRMED")
            check_oidc(c["keycloak"], secrets_value.get("oidc_ca", ""))
            client_changed = ensure_keycloak_client(c, secrets_value)
            report = configure(api, c, secrets_value, m.params["state_directory"])
            report["changed"] = report["changed"] or client_changed
        m.exit_json(changed=bool(report.pop("changed", False)), report=report)
    except HarborError as exc:
        m.fail_json(msg="Harbor endpoint operation blocked", reason_code=exc.code)
    except Exception:
        m.fail_json(
            msg="Harbor endpoint operation failed",
            reason_code="ENDPOINT_OPERATION_FAILED_REMOTE_STATE_UNKNOWN",
        )


if __name__ == "__main__":
    main()
