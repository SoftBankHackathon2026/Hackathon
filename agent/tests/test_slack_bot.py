from __future__ import annotations

from agent.slack_bot import handle_command
from tests.conftest import requires_opa
from tests.test_remediation import make


@requires_opa
def test_commands(cfg, notifier):
    r = make(cfg, notifier=notifier)
    ran: list = []
    run_now = lambda fn: ran.append(fn())  # noqa: E731

    assert "사용법" in handle_command("", "u", r)
    assert "기록이 없다" in handle_command("status", "u", r)
    msg = handle_command("start", "u", r, run_async=run_now)
    assert "시작했다" in msg and ran and ran[0].status == "RECHECK_PASSED"
    status = handle_command("status", "u", r)
    assert "RECHECK_PASSED" in status and "DB_HOST→" in status
    assert "사용법" in handle_command("approve", "u", r)
