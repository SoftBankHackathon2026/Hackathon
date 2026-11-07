#!/usr/bin/python
"""TLS-verified endpoint inspection. A compatible response is not product proof."""

import tempfile
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.wazuh_common import WazuhError, api, check


def main():
    m = AnsibleModule(
        argument_spec={
            "url": {"type": "str", "required": True},
            "ca_file": {"type": "path", "default": ""},
            "ca_pem": {"type": "str", "default": ""},
            "username": {"type": "str", "default": ""},
            "password": {"type": "str", "default": "", "no_log": True},
        },
        supports_check_mode=True,
    )
    report = {
        "kind": "endpoint",
        "checks": [],
        "reason_codes": [],
        "product_confirmed": False,
        "version": None,
    }
    try:
        auth = (
            (m.params["username"], m.params["password"])
            if m.params["password"]
            else None
        )
        with tempfile.TemporaryDirectory(prefix="wazuh-trust-") as directory:
            ca_file = m.params["ca_file"]
            if m.params["ca_pem"]:
                ca_file = str(Path(directory) / "ca.pem")
                Path(ca_file).write_text(m.params["ca_pem"])
            check(bool(ca_file), "TLS_CA_REQUIRED")
            code, data = api(m.params["url"] + "/api/status", ca_file, auth=auth)
        report["http_status"] = code
        report["checks"].append({"name": "endpoint_tls", "status": "pass"})
        report["checks"].append(
            {"name": "endpoint_access", "status": "pass" if code == 200 else "unknown"}
        )
        if code != 200:
            report["reason_codes"].append("ENDPOINT_AUTH_OR_STATUS_UNCONFIRMED")
    except WazuhError as exc:
        report["checks"].append({"name": "endpoint_tls", "status": "fail"})
        report["reason_codes"].append(str(exc))
    m.exit_json(changed=False, report=report)


if __name__ == "__main__":
    main()
