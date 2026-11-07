r"""감사 기록 이관 명령줄 — 실클라우드 시험(T2·T3)과 리허설에서 쓴다.

결과 JSON 은 표준 출력에 쓴다. verify-* 는 invalid 면 종료 코드 1 이다.

  python -m crosscloud.audit_copy.cli aws-to-gcp --source-bucket TRAIL \
      --source-prefix AWSLogs/ --target-bucket ccx-RUN-audit-gcs --run RUN \
      --public-keys keys.json   # aws cloudtrail list-public-keys 출력
  python -m crosscloud.audit_copy.cli verify-aws \
      --target-bucket ccx-RUN-audit-gcs --run RUN
  python -m crosscloud.audit_copy.cli gcp-to-aws --source-bucket COPY_OUT \
      --source-prefix "" --target-bucket ccx-RUN-audit-s3 --run RUN
  python -m crosscloud.audit_copy.cli verify-gcp \
      --target-bucket ccx-RUN-audit-s3 --run RUN
"""

import argparse
import json
import pathlib
import sys
from collections.abc import Sequence
from typing import Any

import boto3
from google.cloud import storage

from crosscloud.audit_copy import archive, archive_files, object_store


def open_s3_bucket(bucket: str) -> object_store.ObjectStore:
    """기본 자격 증명으로 S3 버킷을 연다."""
    return object_store.S3ObjectStore(boto3.client("s3"), bucket)


def open_gcs_bucket(bucket: str) -> object_store.ObjectStore:
    """기본 자격 증명(ADC)으로 GCS 버킷을 연다."""
    return object_store.GcsObjectStore(storage.Client().bucket(bucket))


def main(argv: Sequence[str] | None = None) -> int:
    """명령 하나를 실행하고 종료 코드를 돌려준다."""
    args = _parser().parse_args(argv)
    result = args.run_command(args)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 1 if result.get("result") == "invalid" else 0


def _aws_to_gcp(args: argparse.Namespace) -> dict[str, Any]:
    document = json.loads(pathlib.Path(args.public_keys).read_text("utf-8"))
    return archive.archive_cloudtrail(
        open_s3_bucket(args.source_bucket),
        args.source_prefix,
        open_gcs_bucket(args.target_bucket),
        args.run,
        archive_files.public_keys_from_cli_output(document),
    )


def _gcp_to_aws(args: argparse.Namespace) -> dict[str, Any]:
    return archive.archive_gcp_export(
        open_gcs_bucket(args.source_bucket),
        args.source_prefix,
        open_s3_bucket(args.target_bucket),
        args.run,
    )


def _verify_aws(args: argparse.Namespace) -> dict[str, Any]:
    return archive.verify_cloudtrail_archive(
        open_gcs_bucket(args.target_bucket), args.run
    )


def _verify_gcp(args: argparse.Namespace) -> dict[str, Any]:
    return archive.verify_gcp_archive(
        open_s3_bucket(args.target_bucket), args.run
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crosscloud.audit_copy.cli")
    commands = parser.add_subparsers(required=True)
    for name, run_command, is_copy in (
        ("aws-to-gcp", _aws_to_gcp, True),
        ("gcp-to-aws", _gcp_to_aws, True),
        ("verify-aws", _verify_aws, False),
        ("verify-gcp", _verify_gcp, False),
    ):
        command = commands.add_parser(name)
        command.set_defaults(run_command=run_command)
        command.add_argument("--target-bucket", required=True)
        command.add_argument("--run", required=True)
        if is_copy:
            command.add_argument("--source-bucket", required=True)
            command.add_argument("--source-prefix", required=True)
        if name == "aws-to-gcp":
            command.add_argument(
                "--public-keys",
                required=True,
                help="aws cloudtrail list-public-keys 출력 JSON 파일",
            )
    return parser


if __name__ == "__main__":
    sys.exit(main())
