import base64
import dataclasses

import pytest

from crosscloud.audit_copy import archive_files, cloudtrail_verify, copier

_DIGEST = copier.CopiedObject(
    source_uri="s3://trail/AWSLogs/1/CloudTrail-Digest/d.json.gz",
    target_uri="gs://arc/ccx-r1/objects/AWSLogs/1/CloudTrail-Digest/d.json.gz",
    sha256="a" * 64,
    size=10,
    metadata=(("signature", "abcd"), ("signature-algorithm", "SHA256withRSA")),
)
_LOG = copier.CopiedObject(
    source_uri="s3://trail/AWSLogs/1/CloudTrail/l.json.gz",
    target_uri="gs://arc/ccx-r1/objects/AWSLogs/1/CloudTrail/l.json.gz",
    sha256="b" * 64,
    size=20,
    metadata=(),
)
_KEY = archive_files.PublicKey(
    fingerprint="f" * 32,
    value=b"\x30\x82der",
    validity_start="2026-11-01T00:00:00Z",
    validity_end="2026-12-01T00:00:00Z",
)


def test_bundle_round_trips_objects_signatures_and_keys():
    document = archive_files.bundle_to_json("r1", (_DIGEST, _LOG), (_KEY,))

    bundle = archive_files.bundle_from_json(document)

    # 메타데이터는 객체 목록이 아니라 digest_signatures 로 간다.
    assert bundle.objects == (dataclasses.replace(_DIGEST, metadata=()), _LOG)
    assert bundle.signatures == {
        _DIGEST.source_uri: cloudtrail_verify.DigestSignature(
            signature="abcd", algorithm="SHA256withRSA"
        )
    }
    assert bundle.public_keys == {_KEY.fingerprint: _KEY.value}


def test_bundle_document_names_format_direction_and_path_map():
    document = archive_files.bundle_to_json("r1", (_LOG,), (_KEY,))

    assert document["format"] == archive_files.BUNDLE_FORMAT
    assert document["direction"] == "aws_to_gcp"
    assert document["run_id"] == "r1"
    assert document["objects"] == [
        {
            "source": _LOG.source_uri,
            "target": _LOG.target_uri,
            "sha256": "b" * 64,
            "size": 20,
        }
    ]
    assert (
        document["public_keys"][0]["value"]
        == base64.b64encode(_KEY.value).decode()
    )


def test_manifest_marks_no_provider_evidence_and_its_limit():
    document = archive_files.manifest_to_json("r1", (_LOG,))

    assert document["format"] == archive_files.MANIFEST_FORMAT
    assert document["direction"] == "gcp_to_aws"
    assert document["evidence"] == "none"
    assert document["limitation"] == archive_files.GCP_LIMITATION
    assert archive_files.manifest_from_json(document) == (
        # 매니페스트는 메타데이터를 싣지 않는다.
        dataclasses.replace(_LOG, metadata=()),
    )


@pytest.mark.parametrize(
    "document",
    [
        None,
        {},
        {"format": "other/v1"},
        {"format": archive_files.BUNDLE_FORMAT, "objects": "x"},
        {
            "format": archive_files.BUNDLE_FORMAT,
            "objects": [{"source": "s3://a/b"}],
            "digest_signatures": [],
            "public_keys": [],
        },
        {
            "format": archive_files.BUNDLE_FORMAT,
            "objects": [],
            "digest_signatures": [],
            "public_keys": [{"fingerprint": "f", "value": "!!"}],
        },
    ],
)
def test_malformed_bundle_is_rejected(document):
    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive_files.bundle_from_json(document)


def test_malformed_manifest_is_rejected():
    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive_files.manifest_from_json(
            {"format": archive_files.BUNDLE_FORMAT}
        )


def test_public_keys_from_aws_cli_list_public_keys_output():
    # `aws cloudtrail list-public-keys` 출력: Value 는 base64, 시각은 문자열.
    output = {
        "PublicKeyList": [
            {
                "Value": base64.b64encode(b"der-bytes").decode(),
                "ValidityStartTime": "2026-11-01T00:00:00+00:00",
                "ValidityEndTime": "2026-12-01T00:00:00+00:00",
                "Fingerprint": "f" * 32,
            }
        ]
    }

    keys = archive_files.public_keys_from_cli_output(output)

    assert keys == (
        archive_files.PublicKey(
            fingerprint="f" * 32,
            value=b"der-bytes",
            validity_start="2026-11-01T00:00:00+00:00",
            validity_end="2026-12-01T00:00:00+00:00",
        ),
    )


@pytest.mark.parametrize(
    "output",
    [None, {}, {"PublicKeyList": [{"Value": "!!", "Fingerprint": "f"}]}],
)
def test_malformed_public_keys_output_is_rejected(output):
    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive_files.public_keys_from_cli_output(output)


_RECEIPT = archive_files.Receipt(
    direction="aws_to_gcp",
    run_id="r1",
    archive_file="gs://arc/ccx-r1/bundle.json",
    archive_file_sha256="c" * 64,
    object_count=2,
    chains={
        "AWSLogs/1/CloudTrail-Digest/r/x": cloudtrail_verify.ChainBounds(
            oldest="s3://trail/old.json.gz", newest="s3://trail/new.json.gz"
        )
    },
)


def test_receipt_round_trips_and_names_the_chain_end():
    document = archive_files.receipt_to_json(_RECEIPT)

    assert document["format"] == archive_files.RECEIPT_FORMAT
    assert document["chains"][0]["end_digest"] == "s3://trail/new.json.gz"
    assert archive_files.receipt_from_json(document) == _RECEIPT


@pytest.mark.parametrize(
    "change",
    [
        {"format": "other"},
        {"direction": "sideways"},
        {"object_count": "2"},
        {"archive_file_sha256": 5},
        {"chains": [{"chain": "c"}]},
    ],
)
def test_malformed_receipt_is_rejected(change):
    document = {**archive_files.receipt_to_json(_RECEIPT), **change}

    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive_files.receipt_from_json(document)


def test_bundle_with_duplicate_source_is_rejected():
    document = archive_files.bundle_to_json("r1", (_LOG, _LOG), (_KEY,))

    with pytest.raises(archive_files.InvalidArchiveFileError, match="중복"):
        archive_files.bundle_from_json(document)


def test_manifest_with_duplicate_target_is_rejected():
    other = dataclasses.replace(_LOG, source_uri="gs://src/other")
    document = archive_files.manifest_to_json("r1", (_LOG, other))

    with pytest.raises(archive_files.InvalidArchiveFileError, match="중복"):
        archive_files.manifest_from_json(document)


@pytest.mark.parametrize(
    ("field", "value"), [("sha256", 1), ("size", "20"), ("source", None)]
)
def test_bundle_row_with_wrong_type_is_rejected(field, value):
    document = archive_files.bundle_to_json("r1", (_LOG,), (_KEY,))
    document["objects"][0][field] = value

    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive_files.bundle_from_json(document)


def test_bundle_signature_with_wrong_type_is_rejected():
    document = archive_files.bundle_to_json("r1", (_DIGEST,), (_KEY,))
    document["digest_signatures"][0]["signature"] = 12

    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive_files.bundle_from_json(document)


def test_public_key_files_merge_and_reject_conflicts():
    first = archive_files.public_keys_from_cli_output(_cli_output(b"a", "f1"))
    second = archive_files.public_keys_from_cli_output(_cli_output(b"b", "f2"))
    conflict = archive_files.public_keys_from_cli_output(
        _cli_output(b"c", "f1")
    )

    merged = archive_files.merge_public_keys((first, second, first))

    assert {key.fingerprint for key in merged} == {"f1", "f2"}
    with pytest.raises(archive_files.InvalidArchiveFileError, match="f1"):
        archive_files.merge_public_keys((first, conflict))


def _cli_output(value: bytes, fingerprint: str) -> dict:
    return {
        "PublicKeyList": [
            {
                "Value": base64.b64encode(value).decode(),
                "ValidityStartTime": "2026-11-01T00:00:00+00:00",
                "ValidityEndTime": "2026-12-01T00:00:00+00:00",
                "Fingerprint": fingerprint,
            }
        ]
    }


def test_bundle_keeps_backfill_generation_timestamp_of_a_digest():
    # 이관 뒤에는 원위치 S3 메타데이터를 다시 얻을 수 없다. backfill 다이제스트
    # 검증(생성 시각 ±1시간 공개키 재조회)에 필요한 값이라 bundle 에 남긴다.
    backfill = dataclasses.replace(
        _DIGEST,
        metadata=(
            ("backfill-generation-timestamp", "2026-11-07T09:00:00Z"),
            ("signature", "abcd"),
            ("signature-algorithm", "SHA256withRSA"),
        ),
    )

    document = archive_files.bundle_to_json("r1", (backfill,), (_KEY,))

    (entry,) = document["digest_signatures"]
    assert entry["backfill_generation_timestamp"] == "2026-11-07T09:00:00Z"
    assert archive_files.bundle_from_json(document).signatures == {
        _DIGEST.source_uri: cloudtrail_verify.DigestSignature(
            signature="abcd", algorithm="SHA256withRSA"
        )
    }


def test_bundle_omits_backfill_timestamp_for_standard_digest():
    document = archive_files.bundle_to_json("r1", (_DIGEST,), (_KEY,))

    (entry,) = document["digest_signatures"]
    assert "backfill_generation_timestamp" not in entry
