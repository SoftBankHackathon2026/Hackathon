"""전환 연결 명령 — 인계 렌더링, 접수 큐 준비·확인, 예열, 앱 제어 어댑터.

운영자 순서(설계 4.15·4.18):

  render-handoff → (compose 기동) → ingress-start → migration.cloud run
  → READY → prepare-target → ingress-status → migration.cloud run --approve
  → (시연 끝) ingress-drain-check → compose down

serve-adapter 는 그동안 계속 떠 있다. 결과는 JSON 한 줄이고 종료 코드는 0
성공 · 1 실패·거부다. 비밀값(DB 비밀번호·제어 토큰)은 env 에서만 읽고 출력하지
않는다. 단계를 보는 명령(ingress-start·prepare-target·ingress-status)은
원가연 cloud.lock 을 쥔 채 판정한다.
"""

import argparse
import collections.abc
import dataclasses
import datetime
import http.server
import json
import os
import pathlib
import time
from typing import Any

import yaml

from crosscloud import (
    control_adapter,
    cutover_stage,
    datapath,
    handoff,
    health_probe,
    ingress_control,
    ingress_drain,
    inventory_json,
    pg_client,
    target_deploy,
)

COMMANDS = frozenset(
    {
        "render-handoff",
        "ingress-start",
        "serve-adapter",
        "prepare-target",
        "ingress-status",
        "ingress-drain-check",
    }
)
# 예열 뒤 대상 앱 신원 확인. 재배포가 끝난 뒤라 원가연 HTTP 한도와 무관하다.
_PREWARM_SUCCESSES = 3
_PREWARM_DEADLINE_SECONDS = 300.0
_PREWARM_INTERVAL_SECONDS = 5.0
_APP_TIMEOUT_SECONDS = 5.0
# cloud.lock 이나 입력 파일 문제처럼 사용자에게 한 줄로 알릴 실패.
_REPORTED = (
    cutover_stage.CloudLockBusyError,
    cutover_stage.StageUnreadableError,
    handoff.NotReadyError,
    target_deploy.DeployError,
    ValueError,
    KeyError,
    OSError,
)


class RefusedError(Exception):
    """명령을 진행하지 않는다. 메시지가 그 이유다."""


@dataclasses.dataclass(frozen=True)
class Deps:
    """밖과 닿는 부분. 시험이 가짜로 바꾼다."""

    open_app_login: collections.abc.Callable[[pg_client.PgLogin, str], None]
    make_deployer: collections.abc.Callable[
        [target_deploy.DeploySpec], target_deploy.TargetDeployer
    ]
    inspect_app: collections.abc.Callable[..., tuple[str, ...]]
    environ: collections.abc.Mapping[str, str]
    sleep: collections.abc.Callable[[float], None] = time.sleep
    clock: collections.abc.Callable[[], float] = time.monotonic


def _open_app_login(login: pg_client.PgLogin, app_user: str) -> None:
    with pg_client.connect(login) as connection:
        datapath.open_app_login(connection, app_user)


def default_deps() -> Deps:
    """실제 DB·클라우드·HTTP 에 닿는 기본값."""
    return Deps(
        open_app_login=_open_app_login,
        make_deployer=target_deploy.make_deployer,
        inspect_app=health_probe.app_identity_problems,
        environ=os.environ,
    )


def add_commands(commands: Any) -> None:
    """하위 명령 묶음(argparse subparsers)에 전환 연결 명령을 더한다."""
    _add_render(commands)
    start = commands.add_parser(
        "ingress-start", help="접수 큐를 원본 라벨로 돌리고 전달을 연다"
    )
    _add_files(start, control=True)
    _add_state(start)
    start.add_argument(
        "--allow-no-state",
        action="store_true",
        help="원가연 상태 파일이 아직 없을 때 승인 전으로 본다",
    )
    _add_files(
        commands.add_parser(
            "serve-adapter", help="앱 제어 API 어댑터를 127.0.0.1 에서 연다"
        ),
        control=True,
    )
    prepare = commands.add_parser(
        "prepare-target", help="READY 에서 대상 앱을 B 프로필로 예열한다"
    )
    _add_files(prepare, control=False)
    _add_state(prepare)
    prepare.add_argument("--deploy", required=True, type=pathlib.Path)
    prepare.add_argument(
        "--db-ca-file",
        required=True,
        type=pathlib.Path,
        help="대상 DB 서버 CA PEM(DB_SSL_CA 로 넣는다)",
    )
    status = commands.add_parser(
        "ingress-status", help="승인 직전 확인(읽기 전용)"
    )
    _add_files(status, control=True)
    _add_state(status)
    drain = commands.add_parser(
        "ingress-drain-check", help="접수 큐 미처리 0 확인(읽기 전용)"
    )
    drain.add_argument("--ingress-control-url", required=True)
    drain.add_argument("--redis-url", required=True)


def run(args: argparse.Namespace, deps: Deps | None = None) -> int:
    """하위 명령 하나를 실행하고 종료 코드를 돌려준다."""
    deps = deps or default_deps()
    if args.command == "serve-adapter":
        return _serve_adapter(args, deps)
    handlers = {
        "render-handoff": _render_handoff,
        "ingress-start": _ingress_start,
        "prepare-target": _prepare_target,
        "ingress-status": _ingress_status,
        "ingress-drain-check": _ingress_drain_check,
    }
    try:
        ok, body = handlers[args.command](args, deps)
    except RefusedError as error:
        ok, body = False, {"error": str(error)}
    except _REPORTED as error:
        ok, body = False, {"error": f"{type(error).__name__}: {error}"}
    print(json.dumps(body, ensure_ascii=False))
    return 0 if ok else 1


def build_adapter(
    args: argparse.Namespace, environ: collections.abc.Mapping[str, str]
) -> tuple[control_adapter.AdapterConfig, control_adapter.ControlAdapter]:
    """인계 파일과 env 로 어댑터 설정과 어댑터를 만든다."""
    config = _adapter_config(args)
    return config, control_adapter.ControlAdapter(
        config, _ingress(args, environ)
    )


def build_server(args: argparse.Namespace) -> http.server.ThreadingHTTPServer:
    """serve-adapter 의 서버를 만든다(아직 serve 하지 않음)."""
    config, adapter = build_adapter(args, os.environ)
    return control_adapter.make_server(adapter, config.port)


def _add_files(command: argparse.ArgumentParser, *, control: bool) -> None:
    command.add_argument("--cloud-config", required=True, type=pathlib.Path)
    command.add_argument("--ingress-env", required=True, type=pathlib.Path)
    if control:
        command.add_argument("--ingress-control-url", required=True)
    command.add_argument(
        "--target-environment",
        help="대상 앱 DEPLOY_ENV(주면 신원 확인에서 함께 대조)",
    )


def _add_state(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--state-dir",
        required=True,
        type=pathlib.Path,
        help="원가연 migration.cloud 의 --state-dir",
    )


def _add_render(commands: Any) -> None:
    render = commands.add_parser(
        "render-handoff", help="config.cloud.yaml·ingress.env 를 쓴다"
    )
    for name in ("--inventory", "--source-tf", "--target-tf", "--readiness"):
        render.add_argument(name, required=True, type=pathlib.Path)
    render.add_argument(
        "--target-database",
        required=True,
        help="대상 DB 이름. 원본과 이 앱이 붙었던 어떤 DB 와도 달라야 한다",
    )
    render.add_argument("--source-app-user", required=True)
    render.add_argument(
        "--table",
        action="append",
        required=True,
        metavar="SCHEMA.TABLE=PK[,PK...]",
    )
    for name in (
        "--control-url",
        "--connect-url",
        "--source-app-url",
        "--target-app-url",
        "--redis-url",
    ):
        render.add_argument(name, required=True)
    render.add_argument("--out-dir", required=True, type=pathlib.Path)


def _render_handoff(
    args: argparse.Namespace, deps: Deps
) -> tuple[bool, dict[str, Any]]:
    document = json.loads(args.inventory.read_text())
    inventory = inventory_json.inventory_from_json(
        document.get("inventory", document)
    )
    source = handoff.db_from_terraform(
        inventory.cloud, json.loads(args.source_tf.read_text())
    )
    target_cloud = "gcp" if inventory.cloud == "aws" else "aws"
    target = handoff.db_from_terraform(
        target_cloud,
        json.loads(args.target_tf.read_text()),
        database=args.target_database,
    )
    job = handoff.build_job_config(
        inventory,
        source=source,
        target=target,
        source_app_user=args.source_app_user,
        tables=tuple(_table(value) for value in args.table),
        control_url=args.control_url,
        connect_url=args.connect_url,
        now=_now(),
    )
    readiness = tuple(
        handoff.ReadinessItem(item["name"], item["ready"], item["detail"])
        for item in json.loads(args.readiness.read_text())
    )
    config_path, password_envs = handoff.render_cloud_config(
        job, readiness, args.out_dir
    )
    env_path, ingress_envs = handoff.render_ingress_env(
        job,
        (
            handoff.IngressTarget(source.endpoint.cloud, args.source_app_url),
            handoff.IngressTarget(target.endpoint.cloud, args.target_app_url),
        ),
        args.redis_url,
        args.out_dir,
    )
    return True, {
        "job_id": job.job_id,
        "config": str(config_path),
        "ingress_env": str(env_path),
        "secret_envs": sorted(set(password_envs) | set(ingress_envs)),
    }


def _ingress_start(
    args: argparse.Namespace, deps: Deps
) -> tuple[bool, dict[str, Any]]:
    with cutover_stage.cloud_lock(args.state_dir):
        stage, failed = _stage(args, allow_missing=args.allow_no_state)
        if not cutover_stage.cutover_not_begun(stage, failed):
            raise RefusedError(
                f"원가연 단계 {stage}(failed_stage={failed}) — 승인 뒤라 거부"
            )
        code, body = build_adapter(args, deps.environ)[1].start_on_source()
    return code == 200, body


def _prepare_target(
    args: argparse.Namespace, deps: Deps
) -> tuple[bool, dict[str, Any]]:
    config = _adapter_config(args)
    spec = target_deploy.load_deploy_spec(args.deploy)
    profile = target_deploy.TargetProfile(
        secret_env=spec.secret_env,
        plain_env=tuple(
            (kv.key, str(kv.value))
            for kv in handoff.app_tls_env(args.db_ca_file.read_text())
        ),
    )
    admin, app_user = _target_admin(args.cloud_config, deps.environ)
    with cutover_stage.cloud_lock(args.state_dir):
        stage, failed = _stage(args, allow_missing=False)
        if stage != "READY":
            raise RefusedError(f"예열은 READY 에서만 한다(지금 {stage})")
        deps.open_app_login(admin, app_user)
        revision = deps.make_deployer(spec).deploy(profile)
        problems: tuple[str, ...] = ()

        def identity_ok() -> bool:
            nonlocal problems
            problems = deps.inspect_app(
                config.target_app_url,
                database=config.target_database,
                environment=config.target_environment,
                timeout=_APP_TIMEOUT_SECONDS,
            )
            return not problems

        streak = health_probe.consecutive_successes(
            identity_ok,
            needed=_PREWARM_SUCCESSES,
            deadline_seconds=_PREWARM_DEADLINE_SECONDS,
            interval=_PREWARM_INTERVAL_SECONDS,
            clock=deps.clock,
            sleep=deps.sleep,
        )
    if streak < _PREWARM_SUCCESSES:
        raise RefusedError("대상 앱 신원 확인 실패: " + "; ".join(problems))
    return True, {
        "stage": stage,
        "app_login": f"{app_user} LOGIN",
        "revision": revision,
        "target_database": config.target_database,
    }


def _ingress_status(
    args: argparse.Namespace, deps: Deps
) -> tuple[bool, dict[str, Any]]:
    config = _adapter_config(args)
    problems = []
    with cutover_stage.cloud_lock(args.state_dir):
        stage, _ = _stage(args, allow_missing=False)
        if stage != "READY":
            problems.append(f"원가연 단계 {stage}(READY 여야 승인)")
        state = _ingress(args, deps.environ).status()
    source_name = config.ingress_name(config.source)
    if state.mode != "running":
        problems.append(f"접수 큐 mode={state.mode}")
    if state.target != source_name:
        problems.append(f"접수 큐 target={state.target}(원본 {source_name})")
    if state.last_error is not None:
        problems.append(f"접수 큐 last_error={state.last_error}")
    problems.extend(
        deps.inspect_app(
            config.target_app_url,
            database=config.target_database,
            environment=config.target_environment,
            timeout=_APP_TIMEOUT_SECONDS,
        )
    )
    return not problems, {
        "stage": stage,
        "ingress": dataclasses.asdict(state),
        "problems": problems,
        "ready_to_approve": not problems,
    }


def _ingress_drain_check(
    args: argparse.Namespace, deps: Deps
) -> tuple[bool, dict[str, Any]]:
    problems = ingress_drain.drain_problems(
        args.redis_url, _ingress(args, deps.environ)
    )
    return not problems, {"drained": not problems, "problems": list(problems)}


def _serve_adapter(args: argparse.Namespace, deps: Deps) -> int:
    config, adapter = build_adapter(args, deps.environ)
    server = control_adapter.make_server(adapter, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _adapter_config(args: argparse.Namespace) -> control_adapter.AdapterConfig:
    return control_adapter.load_adapter_config(
        args.cloud_config,
        args.ingress_env,
        target_environment=args.target_environment,
    )


def _ingress(
    args: argparse.Namespace, environ: collections.abc.Mapping[str, str]
) -> ingress_control.IngressControl:
    return ingress_control.IngressControl(
        args.ingress_control_url,
        token=environ.get(handoff.INGRESS_CONTROL_TOKEN_ENV) or None,
    )


def _stage(
    args: argparse.Namespace, *, allow_missing: bool
) -> tuple[str, str | None]:
    job_id = control_adapter.read_job_id(args.cloud_config)
    try:
        return cutover_stage.read_cutover_stage(job_id, args.state_dir)
    except cutover_stage.NoCutoverStateError as error:
        if not allow_missing:
            raise RefusedError(
                f"{error} — 승인 전이 맞으면 --allow-no-state"
            ) from None
        return "not_started", None


def _target_admin(
    cloud_config: pathlib.Path, environ: collections.abc.Mapping[str, str]
) -> tuple[pg_client.PgLogin, str]:
    document = yaml.safe_load(cloud_config.read_text())
    target = document["targets"][document["job"]["target"]]
    users = target["users"]
    password_env = users["admin"]["password_env"]
    password = environ.get(password_env)
    if not password:
        raise RefusedError(f"대상 admin 비밀번호 env {password_env} 가 비었다")
    endpoint = pg_client.DbEndpoint(
        host=target["host"],
        port=int(target.get("port", 5432)),
        database=target["dbname"],
        sslmode=target.get("sslmode", "require"),
    )
    login = pg_client.PgLogin(
        endpoint, users["admin"]["user"], password.encode()
    )
    return login, users["app"]["user"]


def _table(value: str) -> handoff.AppTable:
    name, separator, keys = value.partition("=")
    if not separator or not name or not keys:
        raise ValueError(f"--table 은 SCHEMA.TABLE=PK[,PK...] 형식: {value}")
    return handoff.AppTable(name, tuple(keys.split(",")))


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)
