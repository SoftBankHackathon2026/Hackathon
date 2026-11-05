import logging
import time
from contextlib import nullcontext

from controlplane.core import db
from controlplane.domain.models import DeploymentRequest, GitSource, Runtime
from controlplane.execution.catalog_worker import CatalogAwsWorker
from oneport_aws.execution.oneport_ecs_app_model import RegisteredApp
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsRuntime
from oneport_aws.execution.oneport_ecs_session import (
    OperatorSession,
    build_image,
    get_registered_app,
    print_json,
    wait_for_db,
)
from oneport_aws.providers.oneport_ecs.provisioning import ResourceState

LOG = logging.getLogger(__name__)
KEY_LENGTHS = range(16, 129)
SHOW_FIELDS = ("id", "runtime", "state", "error_code", "service_url", "events")


def _require_release_key(session: OperatorSession) -> None:
    key = session.args.key
    command = session.args.command
    session.require_flag(
        key and len(key) in KEY_LENGTHS, f"{command} requires a 16..128 character --key"
    )


def _enqueue_source(session: OperatorSession) -> DeploymentRequest:
    session.require_flag(session.args.request, "enqueue requires --request")
    request = DeploymentRequest.model_validate_json(session.args.request.read_text())
    if isinstance(request.source, GitSource):
        request = build_image(session, request)
    return request


def _restore_source(session: OperatorSession) -> DeploymentRequest:
    session.require_flag(
        session.args.release, "restore requires --release (a previous succeeded release)"
    )
    old = session.store.get(session.args.release)
    if not session.owns(old) or old["state"] != "succeeded":
        raise ValueError("이 환경에서 성공했던 Release만 복구할 수 있습니다.")
    return DeploymentRequest.model_validate(old["request"])


def _ensure_database_ready(session: OperatorSession, app: RegisteredApp) -> None:
    if not app.wants_database or app.db_state == ResourceState.READY:
        return
    if not session.args.wait_db:
        raise ValueError(
            f"앱 DB가 준비되지 않았습니다 (db_state={app.db_state}). "
            "--wait-db로 기다리거나 잠시 후 다시 실행하세요."
        )
    waited = wait_for_db(session)
    if waited.db_state != ResourceState.READY:
        raise ValueError(f"DB 준비 실패: {waited.db_error}")


def cmd_enqueue_or_restore(session: OperatorSession) -> int:
    args, restoring = session.args, session.args.command == "restore"
    _require_release_key(session)
    request = _restore_source(session) if restoring else _enqueue_source(session)
    app = get_registered_app(session)
    if request.resources.database != app.database:
        raise ValueError(f"앱 등록의 DB 선택({app.database})과 요청이 다릅니다.")
    _ensure_database_ready(session, app)
    session.settings.validate_request(
        request, extra_refs=session.registry.refs(session.app_id, session.env_id)
    )
    row = session.store.enqueue(
        request, args.key, session.settings.fingerprint(), runtime=Runtime.AWS_ECS
    )
    if restoring:
        session.registry.set_restore(row["id"], args.release)
    result = {"id": row["id"], "state": row["state"], "image": request.source.image}
    print_json(result | ({"restores": args.release} if restoring else {}))
    return 0


def cmd_show(session: OperatorSession) -> int:
    session.require_flag(session.args.release, "show requires --release")
    row = session.store.get(session.args.release)
    if not session.owns(row):
        raise ValueError("Release is not in the registered environment")
    print_json({name: row[name] for name in SHOW_FIELDS})
    return 0


def cmd_worker(session: OperatorSession) -> int:
    session.runtime = OneportEcsRuntime(session.settings)
    session.worker = CatalogAwsWorker(session.store, session.runtime)
    logging.basicConfig(level=logging.INFO)
    once = session.args.once
    with nullcontext() if once else db.pooling():
        while True:
            try:
                session.worker.tick()
            except Exception as exc:  # noqa: BLE001 -- DB/session failure leaves journal for reconciliation
                LOG.error("worker unavailable: %s", type(exc).__name__)
                if once:
                    return 1
            if once:
                return 0
            time.sleep(1)
