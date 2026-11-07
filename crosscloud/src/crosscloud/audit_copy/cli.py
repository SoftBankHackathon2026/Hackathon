r"""감사 기록 이관 명령줄 — 실클라우드 시험(T2·T3)과 리허설에서 쓴다.

이관 명령은 이관 기록(--receipt)을 운영자 PC 에 새 파일로 쓰고, 같은 내용을
표준 출력에 쓴다. 검증 명령은 그 기록과, 운영자가 보관한
`aws cloudtrail list-public-keys` 출력 파일(리전마다 하나, 여러 개 가능)을
신뢰 기준으로 받는다. 대상 버킷 안의 bundle·manifest 는 기록과 맞을 때만
믿는다.

  python -m crosscloud.audit_copy.cli aws-to-gcp --source-bucket TRAIL \
      --source-prefix AWSLogs/ --target-bucket ccx-RUN-audit-gcs --run RUN \
      --public-keys keys-ap-northeast-2.json --receipt receipt-aws.json
  python -m crosscloud.audit_copy.cli verify-aws \
      --target-bucket ccx-RUN-audit-gcs --run RUN \
      --receipt receipt-aws.json --public-keys keys-ap-northeast-2.json
  python -m crosscloud.audit_copy.cli gcp-to-aws --source-bucket COPY_OUT \
      --source-prefix "" --target-bucket ccx-RUN-audit-s3 --run RUN \
      --receipt receipt-gcp.json
  python -m crosscloud.audit_copy.cli verify-gcp \
      --target-bucket ccx-RUN-audit-s3 --run RUN --receipt receipt-gcp.json

종료 코드: 0 valid(이관 명령은 성공), 1 invalid, 2 사용법 오류,
3 no_evidence(어긋난 것은 없지만 서명으로 확인한 객체가 없음 — GCP→AWS
검증은 늘 3).
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

EXIT_INVALID = 1
EXIT_NO_EVIDENCE = 3
_EXIT_CODES = {"invalid": EXIT_INVALID, "no_evidence": EXIT_NO_EVIDENCE}


def open_s3_bucket(bucket: str) -> object_store.ObjectStore:
    """기본 자격 증명으로 S3 버킷을 연다."""
    return object_store.S3ObjectStore(boto3.client("s3"), bucket)


def open_gcs_bucket(bucket: str) -> object_store.ObjectStore:
    """기본 자격 증명(ADC)으로 GCS 버킷을 연다."""
    return object_store.GcsObjectStore(storage.Client().bucket(bucket))


def main(argv: Sequence[str] | None = None) -> int:
    """명령 하나를 실행하고 종료 코드를 돌려준다."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.is_copy and pathlib.Path(args.receipt).exists():
        parser.error(f"이관 기록 파일이 이미 있음: {args.receipt}")
    result = args.run_command(args)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return _EXIT_CODES.get(result.get("result", ""), 0)


def _aws_to_gcp(args: argparse.Namespace) -> dict[str, Any]:
    receipt = archive.archive_cloudtrail(
        open_s3_bucket(args.source_bucket),
        args.source_prefix,
        open_gcs_bucket(args.target_bucket),
        args.run,
        _read_public_keys(args.public_keys),
    )
    return _write_receipt(args.receipt, receipt)


def _gcp_to_aws(args: argparse.Namespace) -> dict[str, Any]:
    receipt = archive.archive_gcp_export(
        open_gcs_bucket(args.source_bucket),
        args.source_prefix,
        open_s3_bucket(args.target_bucket),
        args.run,
    )
    return _write_receipt(args.receipt, receipt)


def _verify_aws(args: argparse.Namespace) -> dict[str, Any]:
    return archive.verify_cloudtrail_archive(
        open_gcs_bucket(args.target_bucket),
        args.run,
        _read_receipt(args.receipt),
        _read_public_keys(args.public_keys),
    )


def _verify_gcp(args: argparse.Namespace) -> dict[str, Any]:
    return archive.verify_gcp_archive(
        open_s3_bucket(args.target_bucket),
        args.run,
        _read_receipt(args.receipt),
    )


def _read_public_keys(
    paths: Sequence[str],
) -> tuple[archive_files.PublicKey, ...]:
    return archive_files.merge_public_keys(
        archive_files.public_keys_from_cli_output(_read_json(path))
        for path in paths
    )


def _read_receipt(path: str) -> archive_files.Receipt:
    return archive_files.receipt_from_json(_read_json(path))


def _write_receipt(path: str, receipt: archive_files.Receipt) -> dict[str, Any]:
    document = archive_files.receipt_to_json(receipt)
    with open(path, "x", encoding="utf-8") as f:
        json.dump(document, f, ensure_ascii=False, indent=2)
    return document


def _read_json(path: str) -> object:
    return json.loads(pathlib.Path(path).read_text("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crosscloud.audit_copy.cli")
    commands = parser.add_subparsers(required=True)
    for name, run_command, is_copy, needs_keys in (
        ("aws-to-gcp", _aws_to_gcp, True, True),
        ("gcp-to-aws", _gcp_to_aws, True, False),
        ("verify-aws", _verify_aws, False, True),
        ("verify-gcp", _verify_gcp, False, False),
    ):
        command = commands.add_parser(name)
        command.set_defaults(run_command=run_command, is_copy=is_copy)
        command.add_argument("--target-bucket", required=True)
        command.add_argument("--run", required=True)
        command.add_argument(
            "--receipt",
            required=True,
            help="이관 기록 파일(대상 버킷 밖). 이관 명령은 새로 쓴다",
        )
        if is_copy:
            command.add_argument("--source-bucket", required=True)
            command.add_argument("--source-prefix", required=True)
        if needs_keys:
            command.add_argument(
                "--public-keys",
                required=True,
                nargs="+",
                help="aws cloudtrail list-public-keys 출력 JSON(리전마다)",
            )
    return parser


if __name__ == "__main__":
    sys.exit(main())
