"""Only fixed module report fields cross the private runner boundary."""

import json
import os

from ansible.plugins.callback import CallbackBase

FIELDS = set(
    "status candidates reason_codes planned_packages mount runtime node_name restart_required trust_activated runtime_pull_verified trusted_ca_pem trusted_ca_sha256 service service_active restart_verified product_confirmed version healthy accounts credential_ref oidc_browser_login_verified".split()
)


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "harbor_events"
    CALLBACK_NEEDS_ENABLED = True

    def record(self, result, failed=False):
        file = os.environ.get("HARBOR_EVENTS_FILE")
        if not file:
            return
        raw = result._result
        report = raw.get("report", {})
        event = {
            "failed": failed,
            "changed": bool(raw.get("changed")),
            "report": {k: v for k, v in report.items() if k in FIELDS}
            if isinstance(report, dict)
            else {},
        }
        code = raw.get("reason_code", "ANSIBLE_TASK_FAILED")
        event["reason_code"] = (
            code
            if isinstance(code, str) and code.replace("_", "").isalnum()
            else "ANSIBLE_TASK_FAILED"
        )
        fd = os.open(
            file, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(event) + "\n")

    def v2_runner_on_ok(self, result):
        self.record(result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        self.record(result, True)

    def v2_runner_on_unreachable(self, result):
        self.record(result, True)
