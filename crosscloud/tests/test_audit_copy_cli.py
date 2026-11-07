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


def test_aws_to_gcp_then_verify_reports_valid(buckets, tmp_path, capsys):
    _fill_trail(buckets)

    copy_code = cli.main(
        [
            "aws-to-gcp",
            "--source-bucket", fixture.BUCKET,
            "--source-prefix", "AWSLogs/",
            "--target-bucket", "ccx-t-audit",
            "--run", "t",
            "--public-keys", str(_public_keys_file(tmp_path)),
        ]
    )  # fmt: skip
    capsys.readouterr()
    verify_code = cli.main(
        ["verify-aws", "--target-bucket", "ccx-t-audit", "--run", "t"]
    )

    assert (copy_code, verify_code) == (0, 0)
    assert json.loads(capsys.readouterr().out)["result"] == "valid"


def test_verify_aws_exits_nonzero_on_tampering(buckets, tmp_path, capsys):
    _fill_trail(buckets)
    cli.main(
        [
            "aws-to-gcp",
            "--source-bucket", fixture.BUCKET,
            "--source-prefix", "AWSLogs/",
            "--target-bucket", "ccx-t-audit",
            "--run", "t",
            "--public-keys", str(_public_keys_file(tmp_path)),
        ]
    )  # fmt: skip
    capsys.readouterr()
    target = buckets[("gs", "ccx-t-audit")]
    key = next(k for k in target.objects if "/CloudTrail/" in k)
    target.objects[key] = object_store.StoredObject(data=b"x")

    code = cli.main(
        ["verify-aws", "--target-bucket", "ccx-t-audit", "--run", "t"]
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["result"] == "invalid"


def test_gcp_to_aws_then_verify_reports_no_evidence(buckets, capsys):
    source = memory_store.MemoryStore("gs", "copy-out")
    source.objects["cloudaudit.googleapis.com/a.json"] = (
        object_store.StoredObject(data=b"[]")
    )
    buckets[("gs", "copy-out")] = source

    cli.main(
        [
            "gcp-to-aws",
            "--source-bucket", "copy-out",
            "--source-prefix", "cloudaudit.googleapis.com/",
            "--target-bucket", "ccx-t-audit",
            "--run", "t",
        ]
    )  # fmt: skip
    capsys.readouterr()
    code = cli.main(
        ["verify-gcp", "--target-bucket", "ccx-t-audit", "--run", "t"]
    )

    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["evidence"] == "none"


def test_unknown_command_exits_with_usage_error():
    with pytest.raises(SystemExit) as raised:
        cli.main(["nope"])

    assert raised.value.code == 2
