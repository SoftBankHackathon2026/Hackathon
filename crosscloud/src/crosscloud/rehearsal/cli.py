r"""리허설 명령줄. 자동 단계는 --dry-run 으로 계획만 찍을 수 있다.

  python -m crosscloud.rehearsal --state-dir DIR init --run r1107a \
      --source-image docker.io/kty1021/sample-app@sha256:...
  python -m crosscloud.rehearsal --state-dir DIR status
  python -m crosscloud.rehearsal --state-dir DIR aws-source [--dry-run]
  python -m crosscloud.rehearsal --state-dir DIR cutover-forward
  eval "$(python -m crosscloud.rehearsal --state-dir DIR env forward)"

종료 코드: 0 성공 · 1 단계 실패(한 줄 사유, 비밀값 없음) · 2 인자 오류.
"""

import argparse
import pathlib
import sys
from collections.abc import Sequence

import botocore.exceptions
import google.api_core.exceptions
import google.auth.exceptions
import psycopg

from crosscloud import cutover_stage, datapath, pg_client
from crosscloud.provision import residue, terraform
from crosscloud.rehearsal import commands, config, dbprep, runner, steps, wiring
from crosscloud.secret_transfer import run as secret_run
from crosscloud.secret_transfer import store

_REPORTED = (
    runner.StepError,
    terraform.TerraformError,
    dbprep.PrepError,
    datapath.PrincipalError,
    pg_client.PgToolError,
    secret_run.SecretTransferError,
    store.SecretStoreError,
    store.UnsafeLoggingError,
    residue.ResidueScanError,
    cutover_stage.CloudLockBusyError,
    cutover_stage.StageUnreadableError,
    psycopg.Error,
    botocore.exceptions.BotoCoreError,
    botocore.exceptions.ClientError,
    google.auth.exceptions.GoogleAuthError,
    google.api_core.exceptions.GoogleAPIError,
    OSError,
    ValueError,
)
_STEPS = {step.name: step for step in steps.AUTOMATED}


def main(argv: Sequence[str] | None = None) -> int:
    """명령 하나를 실행하고 종료 코드를 돌려준다."""
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except _REPORTED as error:
        print(
            f"rehearsal: 실패 — {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crosscloud.rehearsal")
    parser.add_argument("--state-dir", required=True, type=pathlib.Path)
    commands_ = parser.add_subparsers(dest="command", required=True)
    init = commands_.add_parser("init", help="상태 디렉터리와 설정을 만든다")
    init.add_argument("--run", required=True)
    init.add_argument(
        "--source-image", required=True, help="팀 CI 이미지 <저장소>@sha256:…"
    )
    init.set_defaults(handler=_init)
    commands_.add_parser("status", help="단계 완료 현황").set_defaults(
        handler=_status
    )
    env = commands_.add_parser("env", help="비밀번호 env 를 채우는 셸 줄")
    env.add_argument("job", choices=config.JOBS)
    env.set_defaults(handler=_env)
    for step in steps.AUTOMATED:
        command = commands_.add_parser(step.name, help=step.run.__doc__)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--force", action="store_true")
        command.set_defaults(handler=_run_step, step=step.name)
    for job in config.JOBS:
        commands_.add_parser(
            f"cutover-{job}", help=f"{job} 전환 명령을 찍는다(사람이 돌림)"
        ).set_defaults(handler=_cutover, job=job)
    return parser


def _init(args: argparse.Namespace) -> None:
    paths = config.StatePaths(args.state_dir)
    cfg = config.RehearsalConfig(run=args.run, source_image=args.source_image)
    if paths.config.exists():
        existing = config.load(paths.config)
        if existing != cfg:
            raise runner.StepError(
                f"이 상태 디렉터리는 run {existing.run} 의 것이다"
            )
        print(f"이미 있다: {paths.config}")
        return
    paths.root.mkdir(parents=True, exist_ok=True)
    config.save(cfg, paths.config)
    print(f"설정을 썼다: {paths.config} (접두사 {cfg.prefix})")


def _status(args: argparse.Namespace) -> None:
    ctx = _context(args, dry=True)
    for step in steps.AUTOMATED:
        mark = "x" if ctx.paths.done(step.name).exists() else " "
        print(f"[{mark}] {step.name}")


def _env(args: argparse.Namespace) -> None:
    ctx = _context(args, dry=True)
    for line in wiring.password_exports(ctx.cfg, args.job):
        print(line)


def _run_step(args: argparse.Namespace) -> None:
    ctx = _context(args, dry=args.dry_run)
    steps.run_step(ctx, _STEPS[args.step], force=args.force)


def _cutover(args: argparse.Namespace) -> None:
    ctx = _context(args, dry=True)
    for line in commands.cutover(ctx.cfg, ctx.paths, args.job):
        print(line)


def _context(args: argparse.Namespace, *, dry: bool) -> steps.Context:
    step_runner: runner.Runner = (
        runner.DryRunner() if dry else runner.RealRunner(args.state_dir)
    )
    return steps.Context.open(args.state_dir, step_runner, dry=dry)
