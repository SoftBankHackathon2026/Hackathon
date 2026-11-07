#!/usr/bin/python
"""Read-only endpoint discovery with CA validation and no redirects."""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.keycloak_common import endpoint_info


def main():
    module = AnsibleModule(
        argument_spec={
            "base_url": {"type": "str", "required": True},
            "realm": {"type": "str", "default": "master"},
            "ca_file": {"type": "path"},
            "management_url": {"type": "str"},
        },
        supports_check_mode=True,
    )
    module.exit_json(changed=False, report=endpoint_info(**module.params))


if __name__ == "__main__":
    main()
