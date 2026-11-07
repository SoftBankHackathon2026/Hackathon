#!/usr/bin/python
from ansible.module_utils.bao_common import BaoError, Client
from ansible.module_utils.basic import AnsibleModule


def main():
    m = AnsibleModule(
        argument_spec={
            "endpoint": {"type": "str", "required": True},
            "ca_file": {"type": "path", "default": None},
        },
        supports_check_mode=True,
    )
    try:
        h = Client(m.params["endpoint"], m.params["ca_file"]).health()
        report = {
            k: h[k]
            for k in ("initialized", "sealed", "standby", "version", "cluster_id")
            if k in h
        }
        report.update(status="inspected", product_confirmed=False)
        # Health endpoint is compatible with Vault; it cannot establish product identity.
        m.exit_json(changed=False, report=report)
    except BaoError as exc:
        m.fail_json(msg="Endpoint inspection blocked", reason_code=str(exc))
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        m.fail_json(
            msg="Endpoint inspection failed", reason_code="ENDPOINT_INSPECTION_FAILED"
        )


if __name__ == "__main__":
    main()
