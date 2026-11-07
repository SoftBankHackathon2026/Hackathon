#!/usr/bin/python
import os
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule


def main():
    m = AnsibleModule(
        argument_spec={
            "owner": {"type": "str", "required": True},
            "action": {
                "type": "str",
                "choices": ["acquire", "release"],
                "required": True,
            },
        }
    )
    directory = Path("/var/lock/openbao-infra.lock")
    marker = directory / "owner"
    try:
        if m.params["action"] == "acquire":
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                m.fail_json(
                    msg="Another operation or interrupted operation holds the lock",
                    reason_code="CONCURRENT_OR_INTERRUPTED_OPERATION",
                )
            fd = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "w") as stream:
                stream.write(m.params["owner"])
        else:
            if directory.exists():
                if directory.is_symlink() or marker.read_text() != m.params["owner"]:
                    m.fail_json(
                        msg="Lock belongs to another operation",
                        reason_code="LOCK_OWNERSHIP_CONFLICT",
                    )
                marker.unlink()
                directory.rmdir()
        m.exit_json(changed=False)
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        m.fail_json(msg="Lock operation failed", reason_code="LOCK_OPERATION_FAILED")


if __name__ == "__main__":
    main()
