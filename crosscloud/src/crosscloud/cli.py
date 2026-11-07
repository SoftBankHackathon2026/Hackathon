r"""crosscloud 명령줄 진입점.

  crosscloud discover --direction aws_to_gcp --region ap-northeast-2 \
      --repo request-api=../sample-app --schema-sql schema.sql

discover 는 원본 클라우드를 읽고(파악) 서비스 대응표로 판정한 결과를 JSON
하나({"inventory": ..., "report": ...})로 낸다. 사람 결정(report.decisions)이
남아 있으면 결과를 다 낸 뒤 종료 코드 3 으로 멈춘다 — 원가연 전환 도구가
승인을 기다릴 때 쓰는 코드와 같다.
"""

import argparse
import dataclasses
import json
import pathlib
import sys
from collections.abc import Callable, Sequence
from typing import Any

import botocore.exceptions
import google.auth.exceptions
import requests

from crosscloud import inventory_json, model
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
        discover 하위 명령을 가진 해석기.
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
    return parser


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
        종료 코드. 0 사람 결정 없음, 1 실행 실패, 3 사람 결정 남음.
        인자 오류는 argparse 가 2 로 끝낸다.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
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
    for name, path in repos.items():
        try:
            found = code_scan.scan_couplings(name, path)
        except code_scan.NoSourceFilesError as error:
            # 검사하지 못한 워크로드는 code_repo 를 비워 source_not_scanned 로
            # 판정되게 둔다.
            print(f"crosscloud: 경고 — {error}", file=sys.stderr)
            continue
        scanned[name] = path
        couplings.extend(found)
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
    )
