"""Persist a module-owned report on the controller while keeping no_log enabled."""

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

from ansible.plugins.action import ActionBase


def native(value):
    if isinstance(value, Mapping):
        return {str(k): native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [native(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("REPORT_TYPE_INVALID")


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    def run(self, tmp=None, task_vars=None):
        super().run(tmp, task_vars)
        report = native(self._task.args.get("report", {}))
        if not isinstance(report, dict) or not report:
            report = {
                "kind": "runner",
                "reason_codes": ["MODULE_REPORT_MISSING"],
                "checks": [{"name": "module_report", "status": "fail"}],
            }
        root = Path(os.environ["WI_REPORT_DIR"])
        path = root / (str(time.time_ns()) + ".json")
        with open(
            path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)
        ) as stream:
            json.dump(report, stream, allow_nan=False)
        failed = bool(self._task.args.get("enforce")) and (
            bool(report.get("reason_codes"))
            or any(c.get("status") == "fail" for c in report.get("checks", []))
        )
        return {
            "changed": False,
            "failed": failed,
            "msg": "REPORT_CHECK_FAILED" if failed else "REPORT_SAVED",
        }
