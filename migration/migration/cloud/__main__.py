"""python -m migration.cloud --config cloud.yaml plan|run|status"""

import argparse
import fcntl
import json
import sys
from contextlib import ExitStack
from pathlib import Path

from ..cdc import ConnectClient, mask_config
from ..cutover import ApprovalRequired
from ..state import CutoverStore
from ..worker_client import HttpWorkerClient
from .plan import connectors, load
from .postgres import Postgres
from .runner import Runner


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PostgreSQL app migration across any configured endpoints"
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("command", choices=("plan", "run", "status"))
    parser.add_argument("--approve")
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = load(args.config)
        if args.command == "plan":
            print(
                json.dumps(
                    {
                        "job": plan.config.job.job_id,
                        "source": plan.config.job.source,
                        "target": plan.config.job.target,
                        "tables": [t.name for t in plan.tables],
                        "manifest": plan.fingerprint,
                        "connectors": {
                            n: mask_config(d) for n, d in connectors(plan).items()
                        },
                    },
                    indent=2,
                )
            )
            return 0
        directory = Path(args.state_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (directory / "cloud.lock").open("a") as lock, ExitStack() as stack:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            store = CutoverStore(directory)
            stack.callback(store.close)
            if args.command == "status":
                job = store.load(plan.config.job.job_id)
                print(json.dumps(job.__dict__ if job else {}, indent=2))
                return 0
            pg = Postgres(plan)
            # Cross-process/state-dir exclusion for each database+table. Acquiring
            # in endpoint order avoids deadlock for reversed source/target jobs.
            for target in sorted((plan.config.job.source, plan.config.job.target)):
                conn = stack.enter_context(pg.connect(target))
                for table in plan.tables:
                    acquired = conn.execute(
                        "SELECT pg_try_advisory_lock(hashtextextended(current_database() || %s,0))",
                        ("migration:" + table.name,),
                    ).fetchone()[0]
                    if not acquired:
                        raise RuntimeError("another migration owns this table")
            runner = Runner(
                plan,
                store,
                pg,
                ConnectClient(plan.config.kafka.connect_url),
                HttpWorkerClient(plan.application_url),
                approve=args.approve,
            )
            result = runner.run(retry=args.retry)
            print(
                json.dumps(
                    {"stage": result.stage, "checkpoint": result.checkpoint}, indent=2
                )
            )
            return 0
    except ApprovalRequired as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except Exception as exc:
        # Exceptions from SQL/HTTP may embed sensitive row values or credentials.
        print(
            f"cloud migration stopped: {type(exc).__name__}; inspect status and private service logs",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
