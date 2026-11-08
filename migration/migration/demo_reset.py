"""Opt-in reset of the two demonstration tables; never changes Kubernetes resources.

The operator CLI stops the bot and both receipt processes first. Existing rows
are archived in their own database before TRUNCATE. Other schemas, databases,
Kafka deployments, and historical cutover checkpoints are retained.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re

from psycopg import sql

from . import db
from .cdc import ConnectClient
from .config import Config, load_config, SEED_ROWS
from .seed import seed_records, verify_seed

CYCLE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def validate(config: Config, cycle: str) -> None:
    if os.environ.get("DEMO_RESET_ENABLED") != "true":
        raise ValueError("DEMO_RESET_DISABLED")
    if not CYCLE.fullmatch(cycle):
        raise ValueError("INVALID_CYCLE")
    if (config.job.source, config.job.target, config.job.table) != (
        "onprem_return", "aws", "public.migration_demo"
    ):
        raise ValueError("DEMO_SCOPE_MISMATCH")
    if (config.target("onprem_return").dbname, config.target("aws").dbname) != (
        "migration_demo_return", "migration_demo"
    ):
        raise ValueError("DEMO_DATABASE_MISMATCH")
    if config.full_migration is None:
        raise ValueError("FULL_MIGRATION_REQUIRED")


def inspect(config: Config) -> dict:
    active = []
    for proc in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            argv = proc.read_bytes().split(b'\0')
            if b'migration.cutover' in argv and b'run' in argv:
                active.append(int(proc.parent.name))
        except (FileNotFoundError, PermissionError):
            pass
    result = {"connectors": ConnectClient(config.kafka.connect_url).list_connectors(),
              "active_cutovers": active, "databases": {}}
    for name in (config.job.source, config.job.target):
        with db.connect(config.connection(name, "admin")) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            count = conn.execute("SELECT count(*) FROM public.migration_demo").fetchone()[0]
            writable = conn.execute(
                "SELECT has_table_privilege(%s,'public.migration_demo','INSERT')",
                (config.target(name).user_spec("app").user,),
            ).fetchone()[0]
            slots = conn.execute(
                "SELECT slot_name FROM pg_replication_slots WHERE database=current_database() AND active"
            ).fetchall()
            result["databases"][name] = {
                "database": config.target(name).dbname, "rows": count,
                "app_insert": writable, "active_slots": [r[0] for r in slots],
            }
    return result


def require_idle(snapshot: dict) -> None:
    if snapshot.get("active_cutovers"):
        raise ValueError("CUTOVER_PROCESS_ACTIVE")
    if snapshot["connectors"]:
        raise ValueError("CDC_CONNECTORS_EXIST")
    if any(d["active_slots"] for d in snapshot["databases"].values()):
        raise ValueError("ACTIVE_REPLICATION_SLOT")


@contextmanager
def execution_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def command_lease(state_dir: Path):
    """Reporter holds this for claim through result; reset holds a durable gate."""
    root = state_dir / "demo-cycles"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield not (root / "reset-in-progress").exists()


def gate(root: Path, cycle: str, finish: bool = False) -> dict:
    with execution_lock(root):
        marker = root / "reset-in-progress"
        if marker.exists() and marker.read_text() != cycle:
            raise ValueError("OTHER_RESET_IN_PROGRESS")
        if finish:
            if not marker.exists():
                raise ValueError("RESET_GATE_REQUIRED")
            marker.unlink()
        else:
            temporary = root / 'reset-in-progress.tmp'
            with temporary.open("w") as out:
                out.write(cycle)
                out.flush()
                os.fsync(out.fileno())
            temporary.replace(marker)
        return {"cycle": cycle, "reset_gate": not finish}


def require_gate(root: Path, cycle: str) -> None:
    marker = root / "reset-in-progress"
    if not marker.exists() or marker.read_text() != cycle:
        raise ValueError("RESET_GATE_REQUIRED")


def close_writer(config: Config, name: str) -> None:
    role = config.target(name).user_spec("app").user
    with db.connect(config.connection(name, "admin")) as conn:
        conn.execute(sql.SQL("REVOKE INSERT ON public.migration_demo FROM {}").format(sql.Identifier(role)))
        if conn.execute("SELECT has_table_privilege(%s,'public.migration_demo','INSERT')", (role,)).fetchone()[0]:
            raise ValueError("WRITER_PRIVILEGE_REMAINS")


def reset_database(config: Config, name: str, cycle: str) -> None:
    role = config.target(name).user_spec("app").user
    close_writer(config, name)
    with db.connect(config.connection(name, "admin")) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=current_database() AND usename=%s AND pid<>pg_backend_pid()", (role,),
        )
        conn.execute("SET lock_timeout = '5s'")
        conn.execute("CREATE SCHEMA IF NOT EXISTS demo_archive")
        exists = conn.execute("SELECT to_regclass(%s)", (f"demo_archive.{cycle}",)).fetchone()[0]
        if exists is None:
            conn.execute("LOCK TABLE public.migration_demo IN ACCESS EXCLUSIVE MODE")
            conn.execute(sql.SQL("CREATE TABLE {}.{} AS TABLE public.migration_demo").format(
                sql.Identifier("demo_archive"), sql.Identifier(cycle)))
            conn.execute("TRUNCATE public.migration_demo")
            if name == config.job.source:
                # seed.insert_seed commits batches; reset must commit archive,
                # truncate and all 800 seeds together so interruption is replayable.
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO public.migration_demo "
                        "(request_id,run_id,phase,request_no,payload,created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                        [(r.request_id, r.run_id, r.phase, r.request_no, r.payload, r.created_at)
                         for r in seed_records(SEED_ROWS)],
                    )
        expected = SEED_ROWS if name == config.job.source else 0
        if conn.execute("SELECT count(*) FROM public.migration_demo").fetchone()[0] != expected:
            raise ValueError("RESET_REPLAY_STATE_CHANGED")
        if name == config.job.source and verify_seed(conn, config.job.table, seed_records(SEED_ROWS)):
            raise ValueError("SEED_VERIFICATION_FAILED")


def reset(config: Config, cycle: str, root: Path) -> dict:
    require_idle(inspect(config))
    root.mkdir(parents=True, exist_ok=True)
    with (root / "reset.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = root / f"{cycle}.json"
        if marker.exists():
            # A completed cycle is not permission to reset its data again.
            return {"cycle": cycle, "already_reset": True}
        # Close BOTH writers before archiving either side.
        for name in (config.job.source, config.job.target):
            close_writer(config, name)
        for name in (config.job.target, config.job.source):
            reset_database(config, name, cycle)
        result = {"cycle": cycle, "source_rows": SEED_ROWS, "target_rows": 0, "writers": "closed"}
        with marker.open("x") as out:
            json.dump(result, out); out.flush(); os.fsync(out.fileno())
        return result


def open_source(config: Config, cycle: str, root: Path) -> dict:
    if not (root / f"{cycle}.json").exists():
        raise ValueError("RESET_REQUIRED")
    snap = inspect(config)
    require_idle(snap)
    source, target = snap["databases"][config.job.source], snap["databases"][config.job.target]
    if source["rows"] != SEED_ROWS or target["rows"] != 0 or target["app_insert"]:
        raise ValueError("SOURCE_START_STATE_CHANGED")
    with db.connect(config.connection(config.job.source, "admin")) as conn:
        conn.execute(sql.SQL("GRANT INSERT ON public.migration_demo TO {}").format(
            sql.Identifier(config.target(config.job.source).user_spec("app").user)))
    return {"cycle": cycle, "source_rows": SEED_ROWS, "target_rows": 0, "writers": "source_only"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG"))
    p.add_argument("--state-dir", default="/var/lib/worker/demo-cycles")
    p.add_argument("--cycle", required=True)
    p.add_argument("--confirm-reset")
    p.add_argument("command", choices=("inspect", "check-cycle", "begin-reset", "finish-reset", "reset-db", "open-source"))
    args = p.parse_args(argv)
    try:
        config = load_config(args.config)
        validate(config, args.cycle)
        if args.command != "inspect" and args.confirm_reset != args.cycle:
            raise ValueError("EXPLICIT_CYCLE_CONFIRMATION_REQUIRED")
        if args.command == "inspect":
            result = inspect(config)
        elif args.command == "check-cycle":
            if (Path(args.state_dir) / f"{args.cycle}.json").exists():
                raise ValueError("CYCLE_ALREADY_USED")
            result = {"cycle": args.cycle, "unused": True}
        elif args.command in ("begin-reset", "finish-reset"):
            result = gate(Path(args.state_dir), args.cycle, args.command == "finish-reset")
        else:
            require_gate(Path(args.state_dir), args.cycle)
            operation = reset if args.command == "reset-db" else open_source
            result = operation(config, args.cycle, Path(args.state_dir))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        message = str(exc)
        # Provider/connection errors may contain DSNs. Only our fixed codes leave the process.
        code = message if re.fullmatch(r"[A-Z_]+", message) else type(exc).__name__
        print(json.dumps({"error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
