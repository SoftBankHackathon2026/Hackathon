"""정책 판정 CLI. 실행 제어기가 쓰는 gate 를 손으로 돌려보고 판정 이력을 본다.

  python -m migration.policy evaluate --input policies/examples/pre_cutover_allow.json --opa-url http://migration-policy:8181
  python -m migration.policy evaluate --input ... --opa-dir ../policies        # 서버 없이 opa eval
  python -m migration.policy history --job-id forward_001 --state-dir /var/lib/worker
  python -m migration.policy show --decision-id dec-...

종료 코드: 0 allow, 2 deny/hold/invalid (정책이 막음), 3 error (정책 서비스/입력 문제), 1 사용법 오류.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .policy_client import (
    ALLOW,
    ERROR,
    DecisionStore,
    Evaluator,
    OpaCliEvaluator,
    OpaHttpEvaluator,
    gate,
)

EXIT_ALLOW, EXIT_USAGE, EXIT_BLOCKED, EXIT_ERROR = 0, 1, 2, 3


def _evaluator(args: argparse.Namespace) -> Evaluator:
    if args.opa_dir:
        return OpaCliEvaluator(args.opa_dir)
    url = args.opa_url or os.environ.get("POLICY_OPA_URL")
    if not url:
        raise SystemExit("--opa-url 또는 POLICY_OPA_URL, 아니면 --opa-dir 가 필요하다")
    return OpaHttpEvaluator(url, timeout=args.timeout)


def _state_dir(args: argparse.Namespace) -> Path:
    return Path(args.state_dir or os.environ.get("WORKER_STATE_DIR") or "/var/lib/worker")


def cmd_evaluate(args: argparse.Namespace) -> int:
    with open(args.input, encoding="utf-8") as fh:
        policy_input: dict[str, Any] = json.load(fh)
    store = DecisionStore(_state_dir(args))
    try:
        decision = gate(_evaluator(args), store, policy_input)
    finally:
        store.close()
    print(json.dumps(decision.as_dict(), ensure_ascii=False, indent=2))
    if decision.outcome == ALLOW:
        return EXIT_ALLOW
    if decision.outcome == ERROR:
        return EXIT_ERROR
    return EXIT_BLOCKED


def cmd_history(args: argparse.Namespace) -> int:
    store = DecisionStore(_state_dir(args))
    try:
        rows = store.history(args.job_id, limit=args.limit)
    finally:
        store.close()
    print(json.dumps([d.as_dict() for d in rows], ensure_ascii=False, indent=2))
    return EXIT_ALLOW


def cmd_show(args: argparse.Namespace) -> int:
    store = DecisionStore(_state_dir(args))
    try:
        decision = store.get(args.decision_id)
        policy_input = store.input_for(args.decision_id)
    finally:
        store.close()
    if decision is None:
        print(f"판정이 없다: {args.decision_id}", file=sys.stderr)
        return EXIT_USAGE
    print(json.dumps({"decision": decision.as_dict(), "input": policy_input}, ensure_ascii=False, indent=2))
    return EXIT_ALLOW


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m migration.policy", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state-dir", help="판정 기록 디렉터리 (기본 WORKER_STATE_DIR 또는 /var/lib/worker)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("evaluate", help="입력 JSON 을 평가·기록한다")
    ev.add_argument("--input", required=True, help="정책 입력 JSON 파일")
    ev.add_argument("--opa-url", help="OPA 서버 (예: http://migration-policy:8181)")
    ev.add_argument("--opa-dir", help="서버 대신 로컬 opa eval 로 평가할 정책 디렉터리")
    ev.add_argument("--timeout", type=float, default=5.0)
    ev.set_defaults(func=cmd_evaluate)

    hi = sub.add_parser("history", help="작업의 판정 이력")
    hi.add_argument("--job-id", required=True)
    hi.add_argument("--limit", type=int, default=50)
    hi.set_defaults(func=cmd_history)

    sh = sub.add_parser("show", help="판정 하나와 그때의 입력 (재현용)")
    sh.add_argument("--decision-id", required=True)
    sh.set_defaults(func=cmd_show)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
