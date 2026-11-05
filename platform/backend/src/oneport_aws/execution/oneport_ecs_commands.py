import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import DeploymentRequest
from oneport_aws.execution.oneport_ecs_release_commands import (
    cmd_enqueue_or_restore,
    cmd_show,
    cmd_worker,
)
from oneport_aws.execution.oneport_ecs_session import (
    WAIT_POLL_SECONDS,
    OperatorSession,
    build_image,
    get_registered_app,
    log_stderr,
    print_json,
    wait_for_db,
)
from oneport_aws.providers.oneport_ecs import cache, database
from oneport_aws.providers.oneport_ecs import logs as app_logs
from oneport_aws.providers.oneport_ecs.preflight import preflight
from oneport_aws.providers.oneport_ecs.secrets import put_secret

REGISTER_FIELDS = (
    "app_id",
    "environment_id",
    "port",
    "health_path",
    "domain",
    "rule_priority",
    "database",
    "database_mode",
    "db_state",
    "redis",
    "cache_state",
)
REGISTRATION_FIELDS = (
    "port",
    "health_path",
    "domain",
    "rule_priority",
    "database",
    "db_state",
    "db_error",
    "redis",
    "cache_state",
    "cache_error",
)
Handler = Callable[[OperatorSession], int]


def cmd_preflight(session: OperatorSession) -> int:
    report = preflight(session.provider())
    print_json({"ready_for_sdk": report.ready_for_sdk, **asdict(report)})
    return 0 if report.ready_for_sdk else 1


def cmd_logs(session: OperatorSession) -> int:
    args, provider = session.args, session.provider()
    if args.follow:
        for line in app_logs.tail(provider, seconds=args.follow):
            print(json.dumps(line.model_dump(mode="json"), ensure_ascii=False))
    elif args.task:
        lines = app_logs.task_log(provider, args.task, container=args.container)
        print_json([line.model_dump(mode="json") for line in lines])
    elif args.grep:
        print_json(app_logs.query(provider, filter_text=args.grep, minutes=args.minutes))
    else:
        lines = app_logs.recent(provider, minutes=args.minutes, container=args.container)
        print_json([line.model_dump(mode="json") for line in lines])
    return 0


def cmd_build(session: OperatorSession) -> int:
    args = session.args
    session.require_flag(args.request, "build requires --request with a git source")
    built = build_image(session, DeploymentRequest.model_validate_json(args.request.read_text()))
    text = built.model_dump_json(indent=2)
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0


def cmd_delete(session: OperatorSession) -> int:
    session.require_flag(
        session.args.yes, "delete removes the app's service/rule/target groups/role; pass --yes"
    )
    print_json(
        {
            "deleted": session.provider().delete_app(),
            "kept": ["ECR images", "logs", "secrets", "DB"],
        }
    )
    return 0


def cmd_register(session: OperatorSession) -> int:
    args = session.args
    session.require_flag(
        args.port and args.health_path, "register requires --port and --health-path"
    )
    provider = session.provider()
    app = session.registry.register(
        provider.environment,
        port=args.port,
        health_path=args.health_path,
        database=args.database,
        database_mode=args.database_mode,
        shared_ref=session.settings.secret_refs.get("db"),
        redis=args.redis,
        taken=provider.listener_priorities(),
    )
    print_json({name: getattr(app, name) for name in REGISTER_FIELDS})
    return 0


def cmd_db_create(session: OperatorSession) -> int:
    existing = get_registered_app(session)
    app = session.registry.register(
        session.provider().environment,
        port=existing.port,
        health_path=existing.health_path,
        database="postgresql",
    )
    if session.args.wait:
        app = wait_for_db(session)
    note = "" if session.args.wait else "worker가 배포와 별개로 RDS를 준비합니다 (약 5~7분)."
    print_json(
        {
            "app_id": app.app_id,
            "database": app.database,
            "db_state": app.db_state,
            "db_error": app.db_error,
            "note": note,
        }
    )
    return 0


def cmd_db_delete(session: OperatorSession) -> int:
    session.require_flag(
        session.args.yes,
        "db-delete removes oneport-db-<app> (a final snapshot is kept); pass --yes",
    )
    if session.has_active_release():
        raise RequestConflict("진행 중인 Release가 있으면 DB를 삭제하지 않습니다.")
    provider = session.provider()
    snapshot = database.delete(provider, suffix=f"-{int(time.time())}")
    session.registry.reset_db(session.app_id, session.env_id)
    print_json({"deleted": provider.environment.db_instance, "final_snapshot": snapshot})
    return 0


def cmd_cache_delete(session: OperatorSession) -> int:
    session.require_flag(
        session.args.yes, "cache-delete removes oneport-cache-<app> and its users; pass --yes"
    )
    if session.has_active_release():
        raise RequestConflict("진행 중인 Release가 있으면 캐시를 삭제하지 않습니다.")
    provider = session.provider()
    finished = False
    while not finished:
        finished, message = cache.delete_step(provider)
        log_stderr(message)
        if not finished:
            time.sleep(WAIT_POLL_SECONDS)
    session.registry.reset_cache(session.app_id, session.env_id)
    print_json({"deleted": provider.environment.cache_name})
    return 0


def cmd_status(session: OperatorSession) -> int:
    app = session.registry.get(session.app_id, session.env_id)
    print_json(
        {
            "aws": session.provider().status().as_dict(),
            "registration": {name: getattr(app, name) for name in REGISTRATION_FIELDS}
            if app
            else None,
            "secret_bindings": sorted(session.registry.refs(session.app_id, session.env_id)),
        }
    )
    return 0


def cmd_secret_put(session: OperatorSession) -> int:
    binding = session.args.binding
    session.require_flag(binding, "secret-put requires --binding; the value is read from stdin")
    if session.has_active_release():
        raise RequestConflict("진행 중인 Release가 있으면 바인딩을 바꾸지 않습니다.")
    value = sys.stdin.read().rstrip("\n")
    ref = put_secret(session.provider(), binding, value)
    session.registry.set_ref(session.app_id, session.env_id, binding, ref)
    print_json(
        {"binding": binding, "secret": ref.split(":::")[0].rsplit(":", 1)[-1], "pinned": True}
    )
    return 0


AWS_COMMANDS: dict[str, Handler] = {
    "preflight": cmd_preflight,
    "logs": cmd_logs,
    "build": cmd_build,
    "delete": cmd_delete,
}
JOURNAL_COMMANDS: dict[str, Handler] = {
    "register": cmd_register,
    "db-create": cmd_db_create,
    "db-delete": cmd_db_delete,
    "cache-delete": cmd_cache_delete,
    "status": cmd_status,
    "secret-put": cmd_secret_put,
    "enqueue": cmd_enqueue_or_restore,
    "restore": cmd_enqueue_or_restore,
    "show": cmd_show,
    "worker": cmd_worker,
}
COMMANDS = [*AWS_COMMANDS, *JOURNAL_COMMANDS]


def run_command(session: OperatorSession) -> int:
    command = session.args.command
    if command in AWS_COMMANDS:
        return AWS_COMMANDS[command](session)
    session.open_journal()
    return JOURNAL_COMMANDS[command](session)
