"""Slack 슬래시 명령. Socket Mode 라 온프렘 k3s 에 Ingress 가 없어도 된다.

  /migrate start            사전 검사 → (필요 시) AI 수정 → 보고
  /migrate status           최근 기록
  /migrate approve <rem_id> 승인 대기 중인 수정안을 적용

토큰: SLACK_BOT_TOKEN(xoxb-, chat:write·commands), SLACK_APP_TOKEN(xapp-, connections:write).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from .config import AgentConfig, env_secret
from .remediation import Remediator

log = logging.getLogger("agent.slack")


def _summary(rec: dict[str, Any]) -> str:
    lines = [f"*{rec.get('remediation_id')}* · {rec.get('status')} · 시작 {rec.get('started_at', '')[:19]}"]
    if rec.get("precheck_decision"):
        d = rec["precheck_decision"]
        lines.append(f"사전 검사: {d.get('outcome')} {', '.join(d.get('reason_codes') or [])}")
    if rec.get("proposal"):
        ch = ", ".join(f"{c['key']}→{c['new']}" for c in rec["proposal"].get("changes", []))
        lines.append(f"수정안: {ch or '(없음)'}")
    if rec.get("remediation_decision"):
        d = rec["remediation_decision"]
        lines.append(f"적용 판정: {d.get('outcome')} {', '.join(d.get('reason_codes') or [])}")
    if rec.get("recheck_decision"):
        d = rec["recheck_decision"]
        lines.append(f"재검사: {d.get('outcome')} {', '.join(d.get('reason_codes') or [])}")
    if rec.get("error"):
        lines.append(f"오류: {rec['error']}")
    return "\n".join(lines)


def handle_command(text: str, user: str, remediator: Remediator, *, run_async: Callable[[Callable[[], Any]], None] | None = None) -> str:
    """명령 문자열을 해석해 즉시 응답 문자열을 돌려준다. 긴 작업(start/approve)은 run_async 로 넘긴다."""
    parts = text.strip().split()
    sub = parts[0] if parts else "help"
    spawn = run_async or (lambda fn: threading.Thread(target=fn, daemon=True).start())
    if sub == "start":
        spawn(lambda: remediator.run())
        return f"사전 검사를 시작했다 (job {remediator.cfg.job_id}). 진행은 채널에 보고한다."
    if sub == "status":
        rec = remediator.records.latest(remediator.cfg.job_id)
        return _summary(rec) if rec else "기록이 없다."
    if sub == "approve":
        if len(parts) < 2:
            return "사용법: /migrate approve <remediation_id>"
        rem_id = parts[1]
        spawn(lambda: remediator.approve(rem_id, approver=user))
        return f"{rem_id} 승인 처리를 시작했다 (승인자 {user})."
    return "사용법: /migrate start | status | approve <remediation_id>"


def serve(cfg: AgentConfig, remediator: Remediator) -> None:
    try:
        from slack_bolt import App  # type: ignore[import-not-found]
        from slack_bolt.adapter.socket_mode import SocketModeHandler  # type: ignore[import-not-found]
    except ImportError:
        raise SystemExit("slack_bolt 가 없다: pip install 'ghost-agent[slack]'") from None
    bot_token = env_secret(cfg.slack.bot_token_env)
    app_token = env_secret(cfg.slack.app_token_env)
    if not bot_token or not app_token:
        raise SystemExit(f"{cfg.slack.bot_token_env} / {cfg.slack.app_token_env} 환경변수가 필요하다")
    app = App(token=bot_token)

    @app.command("/migrate")
    def _migrate(ack, command, respond):  # type: ignore[no-untyped-def]
        ack()
        user = command.get("user_name") or command.get("user_id") or "unknown"
        respond(handle_command(command.get("text", ""), user, remediator))

    log.info("Slack Socket Mode 시작 (job=%s)", cfg.job_id)
    SocketModeHandler(app, app_token).start()
