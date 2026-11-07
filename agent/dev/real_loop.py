"""실제 Codex + 실제 OPA(rego) + 실제 OpenBao(dev) + 실제 Postgres 로컬 한 바퀴. 앱과 재시작만 가짜다.

  dev/real_loop.sh 가 컨테이너를 띄우고 이 파일을 실행한다. 운영 경로가 아니다.
  Codex 는 CODEX_API_KEY 또는 --codex-home 의 로그인을 쓴다.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile

from agent.config import CodexSettings, ProbeSettings
from agent.fakes import DECLARED, JOB_ID, FakeApps, FakeRestarter
from agent.kv import OpenBaoKv
from agent.notify import StdoutNotifier
from agent.probe import PostgresProber
from agent.proposer import build_proposer
from agent.remediation import Deps, Remediator
from agent.runs import RunStore
from migration.policy_client import DecisionStore, OpaHttpEvaluator
from migration.smoke import run_smoke


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--opa-url", required=True)
    p.add_argument("--bao-addr", required=True)
    p.add_argument("--bao-token", required=True)
    p.add_argument("--db-host", default="localhost")
    p.add_argument("--db-port", default="55432")
    p.add_argument("--codex-home")
    p.add_argument("--codex-timeout", type=float, default=300)
    p.add_argument("--no-auto-apply", action="store_true")
    p.add_argument("--runner", choices=("local", "opensandbox"), default="local")
    p.add_argument("--model")
    p.add_argument("--sandbox-url", default="http://127.0.0.1:8090")
    p.add_argument("--sandbox-api-key-env", default="OPENSANDBOX_SERVER_API_KEY")
    args = p.parse_args()

    plan = {
        "source": "aws", "target": "onprem", "approved_locations": ["busan-local"],
        "limits": {"evidence_ttl_seconds": 300, "write_wait_max_seconds": 30},
        "target_db": {"host": args.db_host, "port": int(args.db_port)},
    }
    kv = OpenBaoKv(args.bao_addr, "secret", "hackathon/sample-app/onprem", args.bao_token)
    apps = FakeApps(kv, good_host=args.db_host, good_port=args.db_port)

    def smoke():
        return run_smoke("pre_cutover", job_id=JOB_ID, source_url=apps.source_url, target_url=apps.target_url,
                         http_timeout=2.0, replication_timeout=2.0, poll_interval=0.1).to_check()

    with tempfile.TemporaryDirectory(prefix="agent-real-") as tmp:
        runs, decisions = RunStore(tmp), DecisionStore(tmp)
        try:
            remediator = Remediator(
                Deps(
                    smoke=smoke, evaluator=OpaHttpEvaluator(args.opa_url), decisions=decisions, kv=kv,
                    proposer=build_proposer(CodexSettings(
                        timeout=args.codex_timeout, home=args.codex_home, runner=args.runner, model=args.model,
                        sandbox_url=args.sandbox_url, sandbox_api_key_env=args.sandbox_api_key_env,
                    )),
                    prober=PostgresProber(ProbeSettings(sslmode="disable")),
                    restarter=FakeRestarter(apps), notifier=StdoutNotifier(), runs=runs,
                ),
                job_id=JOB_ID, plan=plan, declared_evidence=DECLARED, auto_apply=not args.no_auto_apply,
            )
            run = remediator.run()
            if run.state == "WAITING_APPROVAL":
                print("--- approve ---")
                run = remediator.approve(run.run_id)
            print(json.dumps({"state": run.state, **run.detail, "events": runs.events(run.run_id)}, ensure_ascii=False, indent=2))
            return 0 if run.state == "RESOLVED" else 2
        finally:
            runs.close()
            decisions.close()
            apps.close()


if __name__ == "__main__":
    sys.exit(main())
