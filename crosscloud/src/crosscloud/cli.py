r"""crosscloud 명령줄 진입점.

  crosscloud discover --direction aws_to_gcp --region ap-northeast-2 \
      --repo request-api=../sample-app --schema-sql schema.sql
  crosscloud ingress-start --cloud-config config.cloud.yaml \
      --ingress-env ingress.env --ingress-control-url http://127.0.0.1:9092
  crosscloud serve-adapter --cloud-config ... --ingress-env ... \
      --ingress-control-url ...

discover 는 원본 클라우드를 읽고(파악) 서비스 대응표로 판정한 결과를 JSON
하나({"inventory": ..., "report": ...})로 낸다. 사람 결정(report.decisions)이
남아 있으면 결과를 다 낸 뒤 종료 코드 3 으로 멈춘다 — 원가연 전환 도구가
승인을 기다릴 때 쓰는 코드와 같다.

ingress-start 는 새로 뜬 접수 큐(durable-ingress)를 원본 라벨로 돌리고 전달을
연다(실패하면 1). 원가연 cloud.lock 을 쥔 채 --state-dir 의 단계를 읽어 승인
전일 때만 한다. 상태 파일이 아직 없으면 --allow-no-state 를 줘야 한다.

serve-adapter 는 config.cloud.yaml 의 control_url(127.0.0.1)에서 원가연 앱
제어 API 어댑터를 연다. 접수 큐 제어 토큰은 INGRESS_CONTROL_TOKEN env
에서만 읽는다.
"""

import argparse
import dataclasses
import http.server
import json
import os
import pathlib
import sys
from collections.abc import Callable, Sequence
from typing import Any

import botocore.exceptions
import google.auth.exceptions
import requests

from crosscloud import (
    control_adapter,
    cutover_stage,
    handoff,
    ingress_control,
    inventory_json,
    model,
)
from crosscloud.discover import InventoryReader, aws, code_scan, gcp
from crosscloud.mapping import classify

_DIRECTIONS: tuple[model.Direction, ...] = ("aws_to_gcp", "gcp_to_aws")
# 결과는 냈지만 사람이 먼저 골라야 할 것이 남았다.
EXIT_DECISIONS_PENDING = 3
# 사용자에게 알리고 1 로 끝낼 실패. 그 밖의 예외는 버그이므로 그대로 올린다.
_REPORTED_ERRORS = (
    OSError,
    ValueError,
    botocore.exceptions.BotoCoreError,
    botocore.exceptions.ClientError,
    google.auth.exceptions.GoogleAuthError,
    gcp.GcpPermissionDeniedError,
    requests.RequestException,
)


def build_parser() -> argparse.ArgumentParser:
    """명령줄 해석기를 만든다.

    Returns:
        discover · ingress-start · serve-adapter 하위 명령을 가진 해석기.
    """
    parser = argparse.ArgumentParser(prog="crosscloud")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser(
        "discover", help="원본 클라우드를 읽고 대상 대응을 판정한다"
    )
    discover.add_argument("--direction", required=True, choices=_DIRECTIONS)
    discover.add_argument("--region", required=True, help="원본 리전")
    discover.add_argument("--project", help="원본 GCP 프로젝트(gcp_to_aws)")
    discover.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="WORKLOAD=PATH",
        help="결합 탐지할 워크로드 소스. 여러 번 줄 수 있다",
    )
    discover.add_argument(
        "--schema-sql",
        type=pathlib.Path,
        help="앱 DB DDL(pg_dump --schema-only). 원가연 지원 제한 판정에 쓴다",
    )
    discover.add_argument(
        "--output", type=pathlib.Path, help="결과 JSON 파일. 없으면 표준 출력"
    )
    start = commands.add_parser(
        "ingress-start", help="접수 큐를 원본 라벨로 돌리고 전달을 연다"
    )
    _add_cutover_arguments(start)
    start.add_argument(
        "--state-dir",
        required=True,
        type=pathlib.Path,
        help="원가연 migration.cloud 의 --state-dir",
    )
    start.add_argument(
        "--allow-no-state",
        action="store_true",
        help="원가연 상태 파일이 아직 없을 때 승인 전으로 본다",
    )
    _add_cutover_arguments(
        commands.add_parser(
            "serve-adapter", help="앱 제어 API 어댑터를 127.0.0.1 에서 연다"
        )
    )
    return parser


def _add_cutover_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--cloud-config", required=True, type=pathlib.Path)
    command.add_argument("--ingress-env", required=True, type=pathlib.Path)
    command.add_argument("--ingress-control-url", required=True)
    command.add_argument(
        "--target-environment",
        help="대상 앱 DEPLOY_ENV(주면 /target 에서 함께 대조)",
    )


def build_adapter(
    args: argparse.Namespace,
) -> tuple[control_adapter.AdapterConfig, control_adapter.ControlAdapter]:
    """인계 파일과 env 로 어댑터 설정과 어댑터를 만든다."""
    config = control_adapter.load_adapter_config(
        args.cloud_config,
        args.ingress_env,
        target_environment=args.target_environment,
    )
    ingress = ingress_control.IngressControl(
        args.ingress_control_url,
        token=os.environ.get(handoff.INGRESS_CONTROL_TOKEN_ENV) or None,
    )
    return config, control_adapter.ControlAdapter(config, ingress)


def build_server(args: argparse.Namespace) -> http.server.ThreadingHTTPServer:
    """serve-adapter 의 서버를 만든다(아직 serve 하지 않음)."""
    config, adapter = build_adapter(args)
    return control_adapter.make_server(adapter, config.port)


def make_default_reader(args: argparse.Namespace) -> InventoryReader:
    """방향에 맞는 실제 클라우드 읽기 객체를 만든다.

    Args:
        args: discover 인자.

    Returns:
        aws_to_gcp 면 AwsInventoryReader, gcp_to_aws 면 GcpInventoryReader.
    """
    if args.direction == "aws_to_gcp":
        return aws.AwsInventoryReader(region=args.region)
    return gcp.GcpInventoryReader(project=args.project, region=args.region)


def main(
    argv: Sequence[str] | None = None,
    make_reader: Callable[
        [argparse.Namespace], InventoryReader
    ] = make_default_reader,
) -> int:
    """명령을 실행한다.

    Args:
        argv: 인자. None 이면 sys.argv[1:].
        make_reader: 인자로 읽기 객체를 만든다. 시험이 가짜로 바꾼다.

    Returns:
        종료 코드. discover: 0 사람 결정 없음, 1 실행 실패, 3 사람 결정 남음.
        ingress-start: 0 성공, 1 실패. serve-adapter: 멈추면 0.
        인자 오류는 argparse 가 2 로 끝낸다.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "ingress-start":
        return _ingress_start(args)
    if args.command == "serve-adapter":
        return _serve_adapter(args)
    if args.direction == "gcp_to_aws" and not args.project:
        parser.error("gcp_to_aws 에는 --project 가 필요하다")
    try:
        document = _discover(args, make_reader)
        _write(document, args.output)
    except _REPORTED_ERRORS as error:
        print(f"crosscloud: 실패 — {error}", file=sys.stderr)
        return 1
    if document["report"]["decisions"]:
        return EXIT_DECISIONS_PENDING
    return 0


def _ingress_start(args: argparse.Namespace) -> int:
    try:
        with cutover_stage.cloud_lock(args.state_dir):
            refusal = _stage_refusal(args)
            if refusal:
                code, body = 409, {"error": refusal}
            else:
                code, body = build_adapter(args)[1].start_on_source()
    except (
        cutover_stage.CloudLockBusyError,
        cutover_stage.StageUnreadableError,
        ValueError,
        OSError,
    ) as error:
        code, body = 409, {"error": str(error)}
    print(json.dumps(body, ensure_ascii=False))
    return 0 if code == 200 else 1


def _stage_refusal(args: argparse.Namespace) -> str | None:
    job_id = control_adapter.read_job_id(args.cloud_config)
    try:
        stage, failed = cutover_stage.read_cutover_stage(job_id, args.state_dir)
    except cutover_stage.NoCutoverStateError as error:
        if not args.allow_no_state:
            return f"{error} — 승인 전이 맞으면 --allow-no-state"
        stage, failed = "not_started", None
    except cutover_stage.StageUnreadableError as error:
        return str(error)
    if not cutover_stage.cutover_not_begun(stage, failed):
        return f"원가연 단계 {stage}(failed_stage={failed}) — 승인 뒤라 거부"
    return None


def _serve_adapter(args: argparse.Namespace) -> int:
    server = build_server(args)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _write(document: dict[str, Any], output: pathlib.Path | None) -> None:
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        sys.stdout.write(text)
    else:
        output.write_text(text, encoding="utf-8")


def _discover(
    args: argparse.Namespace,
    make_reader: Callable[[argparse.Namespace], InventoryReader],
) -> dict[str, Any]:
    repos = _parse_repos(args.repo)
    schema_sql = (
        None
        if args.schema_sql is None
        else args.schema_sql.read_text(encoding="utf-8")
    )
    inventory = _with_couplings(make_reader(args).read(), repos)
    report = classify.classify(inventory, args.direction, schema_sql=schema_sql)
    return {
        "inventory": inventory_json.inventory_to_json(inventory),
        "report": dataclasses.asdict(report),
    }


def _parse_repos(values: list[str]) -> dict[str, pathlib.Path]:
    repos = {}
    for value in values:
        name, separator, path = value.partition("=")
        if not separator or not name or not path:
            raise ValueError(f"--repo 는 WORKLOAD=PATH 형식이다: {value}")
        repos[name] = pathlib.Path(path)
    return repos


def _with_couplings(
    inventory: model.Inventory, repos: dict[str, pathlib.Path]
) -> model.Inventory:
    names = {workload.name for workload in inventory.workloads}
    unknown = sorted(set(repos) - names)
    if unknown:
        raise ValueError(f"inventory 에 없는 워크로드: {', '.join(unknown)}")
    scanned: dict[str, pathlib.Path] = {}
    couplings: list[model.InfraCoupling] = []
    omissions: list[model.Omission] = []
    for name, path in repos.items():
        try:
            scan = code_scan.scan_couplings(name, path)
        except code_scan.NoSourceFilesError as error:
            # 검사하지 못한 워크로드는 code_repo 를 비워 source_not_scanned 로
            # 판정되게 둔다.
            print(f"crosscloud: 경고 — {error}", file=sys.stderr)
            continue
        scanned[name] = path
        couplings.extend(scan.couplings)
        # 크기 제한으로 읽지 않은 파일은 사람이 확인한다.
        omissions.extend(
            model.Omission(
                f"source_skipped:{name}/{relative}", "unsupported_type", None
            )
            for relative in scan.skipped_large_files
        )
    workloads = tuple(
        dataclasses.replace(workload, code_repo=str(scanned[workload.name]))
        if workload.name in scanned
        else workload
        for workload in inventory.workloads
    )
    return dataclasses.replace(
        inventory,
        workloads=workloads,
        couplings=inventory.couplings + tuple(couplings),
        omissions=inventory.omissions + tuple(omissions),
    )
