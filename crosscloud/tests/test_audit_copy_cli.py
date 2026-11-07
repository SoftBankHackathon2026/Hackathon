import base64
import json

import cloudtrail_fixture as fixture
import memory_store
import pytest

from crosscloud.audit_copy import cli, object_store


@pytest.fixture
def buckets(monkeypatch):
    """버킷 이름 → 메모리 버킷. cli 가 실클라우드 대신 이것을 쓴다."""
    stores: dict[tuple[str, str], memory_store.MemoryStore] = {}

    def open_store(scheme):
        def factory(bucket):
            key = (scheme, bucket)
            stores.setdefault(key, memory_store.MemoryStore(scheme, bucket))
            return stores[key]

        return factory

    monkeypatch.setattr(cli, "open_s3_bucket", open_store("s3"))
    monkeypatch.setattr(cli, "open_gcs_bucket", open_store("gs"))
    return stores


def _public_keys_file(tmp_path):
    path = tmp_path / "keys.json"
    path.write_text(
        json.dumps(
            {
                "PublicKeyList": [
                    {
                        "Value": base64.b64encode(
                            fixture.public_key_der()
                        ).decode(),
                        "ValidityStartTime": "2026-11-01T00:00:00+00:00",
                        "ValidityEndTime": "2026-12-01T00:00:00+00:00",
                        "Fingerprint": fixture.FINGERPRINT,
                    }
                ]
            }
        )
    )
    return path


def _fill_trail(buckets) -> None:
    trail = fixture.chain(2)
    source = buckets.setdefault(
        ("s3", fixture.BUCKET), memory_store.MemoryStore("s3", fixture.BUCKET)
    )
    for uri, data in trail.objects.items():
        metadata = ()
        if uri in trail.signatures:
            metadata = (
                ("signature", trail.signatures[uri]),
                ("signature-algorithm", "SHA256withRSA"),
            )
        key = uri.removeprefix(f"s3://{fixture.BUCKET}/")
        source.objects[key] = object_store.StoredObject(
            data=data, metadata=metadata
        )


def _aws_to_gcp(tmp_path) -> int:
    return cli.main(
        [
            "aws-to-gcp",
            "--source-bucket", fixture.BUCKET,
            "--source-prefix", "AWSLogs/",
            "--target-bucket", "ccx-t-audit-gcs",
            "--run", "t",
            "--public-keys", str(_public_keys_file(tmp_path)),
            "--receipt", str(tmp_path / "receipt.json"),
        ]
    )  # fmt: skip


def _verify_aws(tmp_path, *key_files) -> int:
    key_files = key_files or (_public_keys_file(tmp_path),)
    return cli.main(
        [
            "verify-aws",
            "--target-bucket", "ccx-t-audit-gcs",
            "--run", "t",
            "--receipt", str(tmp_path / "receipt.json"),
            "--public-keys", *map(str, key_files),
        ]
    )  # fmt: skip


def test_aws_to_gcp_writes_receipt_outside_the_bucket(
    buckets, tmp_path, capsys
):
    _fill_trail(buckets)

    code = _aws_to_gcp(tmp_path)

    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert code == 0
    assert json.loads(capsys.readouterr().out) == receipt
    assert receipt["chains"][0]["end_digest"].endswith(".json.gz")


def test_aws_to_gcp_then_verify_reports_valid(buckets, tmp_path, capsys):
    _fill_trail(buckets)
    _aws_to_gcp(tmp_path)
    capsys.readouterr()

    code = _verify_aws(tmp_path)

    assert code == 0
    assert json.loads(capsys.readouterr().out)["result"] == "valid"


def test_verify_aws_accepts_several_public_key_files(buckets, tmp_path, capsys):
    _fill_trail(buckets)
    _aws_to_gcp(tmp_path)
    capsys.readouterr()
    other_region = tmp_path / "other.json"
    other_region.write_text(json.dumps({"PublicKeyList": []}))

    code = _verify_aws(tmp_path, _public_keys_file(tmp_path), other_region)

    assert code == 0


def test_verify_aws_exits_nonzero_on_tampering(buckets, tmp_path, capsys):
    _fill_trail(buckets)
    _aws_to_gcp(tmp_path)
    capsys.readouterr()
    target = buckets[("gs", "ccx-t-audit-gcs")]
    key = next(k for k in target.objects if "/CloudTrail/" in k)
    target.objects[key] = object_store.StoredObject(data=b"x")

    code = _verify_aws(tmp_path)

    assert code == 1
    assert json.loads(capsys.readouterr().out)["result"] == "invalid"


def test_existing_receipt_file_is_not_overwritten(buckets, tmp_path):
    _fill_trail(buckets)
    (tmp_path / "receipt.json").write_text("{}")

    with pytest.raises(SystemExit) as raised:
        _aws_to_gcp(tmp_path)

    assert raised.value.code == 2
    assert (tmp_path / "receipt.json").read_text() == "{}"
    assert ("gs", "ccx-t-audit-gcs") not in buckets


def test_gcp_to_aws_then_verify_reports_no_evidence(buckets, tmp_path, capsys):
    source = memory_store.MemoryStore("gs", "copy-out")
    source.objects["cloudaudit.googleapis.com/a.json"] = (
        object_store.StoredObject(data=b"[]")
    )
    buckets[("gs", "copy-out")] = source
    receipt = str(tmp_path / "receipt.json")

    cli.main(
        [
            "gcp-to-aws",
            "--source-bucket", "copy-out",
            "--source-prefix", "cloudaudit.googleapis.com/",
            "--target-bucket", "ccx-t-audit-s3",
            "--run", "t",
            "--receipt", receipt,
        ]
    )  # fmt: skip
    capsys.readouterr()
    code = cli.main(
        [
            "verify-gcp",
            "--target-bucket", "ccx-t-audit-s3",
            "--run", "t",
            "--receipt", receipt,
        ]
    )  # fmt: skip

    result = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_NO_EVIDENCE
    assert result["result"] == "no_evidence"
    assert result["evidence"] == "none"


def test_unknown_command_exits_with_usage_error():
    with pytest.raises(SystemExit) as raised:
        cli.main(["nope"])

    assert raised.value.code == 2
