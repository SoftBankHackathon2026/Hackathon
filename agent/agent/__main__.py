"""CLI.

  python -m agent run     --config agent.yaml          사전 검사 → AI 수정 → 보고 (한 번)
  python -m agent approve --config agent.yaml <rem_id> 승인 대기 수정안 적용
  python -m agent status  --config agent.yaml          최근 기록
  python -m agent probe   --config agent.yaml          저장소 값으로 탐침만
  python -m agent slack   --config agent.yaml          Slack 봇 (Socket Mode)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import AgentConfig, load_config
from .notify import LogNotifier, Notifier, SlackNotifier
from .probe import PostgresProber
from .proposer import CodexProposer
from .remediation import Remediator, build_evaluators
from .slack_bot import _summary, serve
from .stores import build_store


def build_notifier(cfg: AgentConfig, *, force_log: bool = False) -> Notifier:
    if force_log or not cfg.slack.channel:
        return LogNotifier()
    try:
        return SlackNotifier(cfg.slack.channel, token_env=cfg.slack.bot_token_env)
    except ValueError as exc:
        logging.getLogger("agent").warning("%s → 로그 알림으로 대체", exc)
        return LogNotifier()


def build_remediator(cfg: AgentConfig, *, notifier: Notifier | None = None) -> Remediator:
    mig_eval, rem_eval = build_evaluators(cfg)
    return Remediator(
        cfg, store=build_store(cfg.store), prober=PostgresProber(cfg.scope, timeout=cfg.probe_timeout_seconds),
        proposer=CodexProposer(cfg.codex), notifier=notifier or build_notifier(cfg),
        migration_evaluator=mig_eval, remediation_evaluator=rem_eval,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "approve", "status", "probe", "slack"])
    parser.add_argument("args", nargs="*")
    parser.add_argument("--config", required=True)
    parser.add_argument("--no-slack", action="store_true", help="Slack 대신 로그로 보고")
    parser.add_argument("-v", "--verbose", action="store_true")
    ns = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if ns.verbose else logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = load_config(ns.config)
    if ns.command == "probe":
        snap = build_store(cfg.store).read()
        result = PostgresProber(cfg.scope, timeout=cfg.probe_timeout_seconds).probe(snap.values)
        print(json.dumps(result.as_check(app_id=f"{cfg.target_env}:agent-probe"), ensure_ascii=False, indent=2))
        return 0 if result.db_connected else 1

    rem = build_remediator(cfg, notifier=build_notifier(cfg, force_log=ns.no_slack))
    if ns.command == "run":
        rec = rem.run()
        print(json.dumps(rec.as_dict(), ensure_ascii=False, indent=2))
        return 0 if rec.status in ("NO_ACTION", "RECHECK_PASSED") else 2
    if ns.command == "approve":
        if not ns.args:
            parser.error("approve <remediation_id>")
        rec = rem.approve(ns.args[0], approver="cli")
        print(json.dumps(rec.as_dict(), ensure_ascii=False, indent=2))
        return 0 if rec.status == "RECHECK_PASSED" else 2
    if ns.command == "status":
        data = rem.records.latest(cfg.job_id)
        print(_summary(data) if data else "기록이 없다.")
        return 0
    serve(cfg, rem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
