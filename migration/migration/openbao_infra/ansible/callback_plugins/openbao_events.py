"""Only module-owned report projections cross the platform boundary."""

import json
import os

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "openbao_events"
    CALLBACK_NEEDS_ENABLED = True

    def record(self, result, failed=False):
        path = os.environ.get("OPENBAO_EVENTS_FILE")
        if not path:
            return
        raw = result._result
        report = raw.get("report", {})
        if not isinstance(report, dict):
            report = {}
        event = {
            "host": result._host.get_name(),
            "failed": failed,
            "changed": bool(raw.get("changed")),
            "report": report,
        }
        if failed:
            code = raw.get("reason_code", "ANSIBLE_TASK_FAILED")
            event["reason_code"] = (
                code
                if isinstance(code, str) and code.replace("_", "").isalnum()
                else "ANSIBLE_TASK_FAILED"
            )
        fd = os.open(
            path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(event) + "\n")

    def v2_runner_on_ok(self, result):
        self.record(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self.record(result, True)

    def v2_runner_on_unreachable(self, result):
        self.record(result, True)
