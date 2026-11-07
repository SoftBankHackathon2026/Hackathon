import base64
import dataclasses
import datetime

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


def test_public_keys_from_list_public_keys_response():
    response = {
        "PublicKeyList": [
            {
                "Value": b"der-bytes",
                "ValidityStartTime": datetime.datetime(
                    2026, 11, 1, tzinfo=datetime.UTC
                ),
                "ValidityEndTime": datetime.datetime(
                    2026, 12, 1, tzinfo=datetime.UTC
                ),
                "Fingerprint": "f" * 32,
            }
        ]
    }

    keys = archive_files.public_keys_from_list_response(response)

    assert keys == (
        archive_files.PublicKey(
            fingerprint="f" * 32,
            value=b"der-bytes",
            validity_start="2026-11-01T00:00:00Z",
            validity_end="2026-12-01T00:00:00Z",
        ),
    )
