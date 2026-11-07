"""Only record fixed reason codes and safe task names, never result payloads."""

import json
import os
from pathlib import Path

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "keycloak_events"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self.events = []
        self.changes = []

    def record(self, result, code):
        self.events.append(
            {
                "host": result._host.get_name(),
                "task": result._task.get_name(),
                "reason_code": code,
            }
        )
        target = os.environ.get("KC_EVENTS_FILE")
        if target:
            temporary = target + ".tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.events, f)
            Path(temporary).replace(target)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        codes = [
            tag.removeprefix("kc_reason_")
            for tag in result._task.tags
            if tag.startswith("kc_reason_")
        ]
        self.record(result, codes[0] if codes else "ANSIBLE_TASK_FAILED")

    def v2_runner_on_unreachable(self, result):
        self.record(result, "SSH_OR_CONTROLLER_UNREACHABLE")

    def v2_runner_on_ok(self, result):
        if (
            not result._result.get("changed")
            or result._result.get("_ansible_delegated_vars")
            or not result._host.get_name().startswith("node_")
        ):
            return
        self.changes.append(
            {
                "host": result._host.get_name(),
                "task": result._task.get_name(),
                "action": result._task.action,
            }
        )
        target = os.environ.get("KC_CHANGES_FILE")
        if target:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.changes, f)
