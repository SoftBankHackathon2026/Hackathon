"""Operator-only durable enqueue/worker commands. No public deployment endpoint."""

import argparse
import json
import logging
import time
from contextlib import nullcontext
from pathlib import Path

import psycopg
from kubernetes.config.config_exception import ConfigException
from pydantic import ValidationError

from controlplane.core import db
from controlplane.core.errors import RequestConflict
from controlplane.domain.models import DeploymentRequest
from controlplane.repositories.releases import ReleaseStore

LOG = logging.getLogger(__name__)


SHOW_FIELDS = ("id", "runtime", "state", "error_code", "service_url", "events")
REJECTED_MESSAGE = "Configuration, request or current release state rejected; check operator inputs"
HANDLED_ERRORS = (
    ValidationError,
    ValueError,
    OSError,
    ConfigException,
    RequestConflict,
    psycopg.Error,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["enqueue", "worker", "show"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--release")
    parser.add_argument("--once", action="store_true")
    return parser


def enqueue(args, parser, store, settings, request_type, queue_runtime) -> None:
    if not args.request or not args.key or not 16 <= len(args.key) <= 128:
        parser.error("enqueue requires --request and a 16..128 character --key")
    request = request_type.model_validate_json(args.request.read_text())
    settings.validate_request(request)
    row = store.enqueue(
        request,
        args.key,
        settings.fingerprint(),
        runtime=queue_runtime,
        timeout_seconds=getattr(settings, "release_timeout_seconds", 720),
    )
    print(json.dumps({"id": row["id"], "state": row["state"], "runtime": row["runtime"]}))


def show(args, parser, store, settings, queue_runtime) -> None:
    if not args.release:
        parser.error("show requires --release")
    row = store.get(args.release)
    env = settings.environment
    if (
        row is None
        or row["runtime"] != queue_runtime
        or (row["application"], row["environment_id"]) != (env.application, env.environment_id)
    ):
        raise ValueError("Release is not in the registered environment")
    print(
        json.dumps(
            {key: row[key] for key in SHOW_FIELDS}, ensure_ascii=False, default=str, indent=2
        )
    )


def run_worker(worker, once: bool) -> int:
    logging.basicConfig(level=logging.INFO)
    with nullcontext() if once else db.pooling():
        while True:
            try:
                worker.tick()
            except Exception as exc:  # noqa: BLE001 -- DB/session failure leaves journal for reconciliation
                LOG.error("worker unavailable: %s", type(exc).__name__)
                if once:
                    return 1
            if once:
                return 0
            time.sleep(1)


def main(
    *,
    settings_type,
    runtime_type,
    worker_type,
    request_type=DeploymentRequest,
    queue_runtime,
):
    parser = build_parser()
    args = parser.parse_args()
    runtime = None
    worker = None
    try:
        settings = settings_type.model_validate_json(args.config.read_text())
        store = ReleaseStore(settings.database_url.get_secret_value())
        store.migrate()
        if args.command == "enqueue":
            enqueue(args, parser, store, settings, request_type, queue_runtime)
        elif args.command == "show":
            show(args, parser, store, settings, queue_runtime)
        else:
            runtime = runtime_type(settings)
            worker = worker_type(store, runtime)
            return run_worker(worker, args.once)
        return 0
    except HANDLED_ERRORS:
        print(json.dumps({"error": REJECTED_MESSAGE}))
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if worker and hasattr(worker, "close"):
            worker.close()
        if runtime:
            runtime.close()
