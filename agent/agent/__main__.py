"""python -m agent run | watch [--interval 초] | approve <run_id> | status [run_id]

종료 코드: 0 복구 완료·고칠 것 없음·승인 대기, 1 설정 오류, 2 그 밖의 끝 상태 (사람이 봐야 한다).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from typing import Callable

from migration.policy_client import DecisionStore, OpaHttpEvaluator
from migration.smoke import SmokeConfigError, run_smoke, token_from_env

from .config import AgentConfig, ConfigError, load_config
from .kv import OpenBaoKv
from .notify import build_notifier
from .probe import PostgresProber
from .proposer import ProposerError, build_proposer
from .remediation import Deps, Remediator
from .restart import InClusterKubeApi, RolloutRestarter
from . import runs as R
from .runs import OK_STATES, Run, RunStore

EXIT_OK, EXIT_CONFIG, EXIT_ATTENTION = 0, 1, 2


def smoke_with_retry(
    once: Callable[[], dict], *, retries: int, delay: float, sleep: Callable[[float], None] = time.sleep,
) -> dict:
    check = once()
    for _ in range(max(retries, 0)):
        if check.get("status") != "error":
            break
        logging.getLogger("agent").info("스모크 관측 오류. %.0f초 뒤 다시 본다", delay)
        sleep(delay)
        check = once()
    return check


def build_remediator(cfg: AgentConfig, runs: RunStore, decisions: DecisionStore) -> Remediator:
    def smoke() -> dict:
        s = cfg.smoke
        return smoke_with_retry(lambda: run_smoke(
            s.stage, job_id=cfg.job_id, source_url=s.source_url, target_url=s.target_url, target_only=s.target_only,
            token=token_from_env(s.token_env), http_timeout=s.http_timeout,
            replication_timeout=s.replication_timeout, poll_interval=s.poll_interval,
        ).to_check(), retries=s.error_retries, delay=s.retry_delay)

    deps = Deps(
        smoke=smoke,
        evaluator=OpaHttpEvaluator(cfg.opa_url, timeout=cfg.opa_timeout),
        decisions=decisions,
        kv=OpenBaoKv(cfg.openbao.addr, cfg.openbao.mount, cfg.openbao.path, cfg.openbao.token, timeout=cfg.openbao.timeout),
        proposer=build_proposer(cfg.codex),
        prober=PostgresProber(cfg.probe),
        restarter=RolloutRestarter(cfg.restart, InClusterKubeApi(
            server=cfg.restart.api_server, token_file=cfg.restart.token_file, ca_file=cfg.restart.ca_file,
        )),
        notifier=build_notifier(cfg.slack_channel, thread_ts=cfg.slack_thread_ts),
        runs=runs,
    )
    return Remediator(deps, job_id=cfg.job_id, plan=cfg.plan, declared_evidence=cfg.declared_evidence,
                      auto_apply=cfg.auto_apply, stage=cfg.smoke.stage, approve_command=cfg.approve_command)


BUSY_STATES = frozenset({R.WAITING_APPROVAL, R.APPLYING})
APPLYING_STALE_SECONDS = 900


def _stale(run: Run, at: datetime) -> bool:
    if run.state != R.APPLYING:
        return False
    return (at - datetime.fromisoformat(run.updated_at.replace("Z", "+00:00"))).total_seconds() > APPLYING_STALE_SECONDS


def watch(
    remediator: Remediator, runs: RunStore, *, interval: float, iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    log = logging.getLogger("agent.watch")
    done = 0
    while iterations is None or done < iterations:
        latest = next(iter(runs.recent()), None)
        if latest is not None and latest.state in BUSY_STATES and not _stale(latest, now()):
            log.info("진행 중인 실행 %s (%s) 이 있어 새로 판정하지 않는다", latest.run_id, latest.state)
        else:
            run = remediator.run()
            log.info("실행 %s 끝: %s", run.run_id, run.state)
        done += 1
        if iterations is None or done < iterations:
            sleep(interval)


def _print_run(run: Run, runs: RunStore) -> None:
    print(json.dumps({**dataclasses.asdict(run), "events": runs.events(run.run_id)}, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="설정 파일 (기본 AGENT_CONFIG 또는 /etc/agent/agent.yaml)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="한 번 실행한다")
    r.add_argument("--no-auto-apply", action="store_true", help="저장소에 쓰기 전에 멈추고 승인을 기다린다")
    w = sub.add_parser("watch", help="주기마다 판정하고 APP-01 이면 고친다")
    w.add_argument("--interval", type=float, default=60.0)
    a = sub.add_parser("approve", help="승인 대기 중인 실행을 적용한다")
    a.add_argument("run_id")
    s = sub.add_parser("status", help="실행 기록을 본다")
    s.add_argument("run_id", nargs="?")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(args.config)
        if args.command == "run" and args.no_auto_apply:
            cfg = dataclasses.replace(cfg, auto_apply=False)
    except ConfigError as exc:
        logging.getLogger("agent").error("설정 오류: %s", exc)
        return EXIT_CONFIG

    runs, decisions = RunStore(cfg.state_dir), DecisionStore(cfg.state_dir)
    try:
        if args.command == "status":
            if args.run_id:
                found = runs.get(args.run_id)
                if found is None:
                    print(f"실행 기록이 없다: {args.run_id}", file=sys.stderr)
                    return EXIT_CONFIG
                _print_run(found, runs)
            else:
                for item in runs.recent():
                    print(item.run_id, item.state, item.updated_at)
            return EXIT_OK
        try:
            remediator = build_remediator(cfg, runs, decisions)
        except ProposerError as exc:
            logging.getLogger("agent").error("초기화 실패: %s", exc)
            return EXIT_CONFIG
        except (OSError, SmokeConfigError) as exc:
            logging.getLogger("agent").error("초기화 실패: %s", type(exc).__name__)
            return EXIT_CONFIG
        if args.command == "watch":
            watch(remediator, runs, interval=max(args.interval, 10.0))
            return EXIT_OK
        try:
            run = remediator.run() if args.command == "run" else remediator.approve(args.run_id)
        except ValueError as exc:  # 승인 대상이 없거나 상태가 맞지 않음
            print(str(exc), file=sys.stderr)
            return EXIT_CONFIG
        return EXIT_OK if run.state in OK_STATES else EXIT_ATTENTION
    finally:
        runs.close()
        decisions.close()


if __name__ == "__main__":
    sys.exit(main())
