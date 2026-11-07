import dataclasses
import gzip
import hashlib
import json

import cloudtrail_fixture as fixture
import memory_store
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from crosscloud.audit_copy import archive, archive_files, object_store

_PUBLIC_KEY = archive_files.PublicKey(
    fingerprint=fixture.FINGERPRINT,
    value=fixture.public_key_der(),
    validity_start="2026-11-01T00:00:00Z",
    validity_end="2026-12-01T00:00:00Z",
)
_RUN = "r6t"
_OBJECTS = f"ccx-{_RUN}/objects/"
_BUNDLE = f"ccx-{_RUN}/bundle.json"
_MANIFEST = f"ccx-{_RUN}/manifest.json"


def _trail_bucket(trail: fixture.FakeTrail) -> memory_store.MemoryStore:
    source = memory_store.MemoryStore("s3", fixture.BUCKET)
    for uri, data in trail.objects.items():
        key = uri.removeprefix(f"s3://{fixture.BUCKET}/")
        metadata = ()
        if uri in trail.signatures:
            metadata = (
                ("signature", trail.signatures[uri]),
                ("signature-algorithm", "SHA256withRSA"),
            )
        source.objects[key] = object_store.StoredObject(
            data=data, content_type="application/json", metadata=metadata
        )
    return source


def _archived(
    hours: int = 3, run: str = _RUN
) -> tuple[fixture.FakeTrail, memory_store.MemoryStore, archive_files.Receipt]:
    trail = fixture.chain(hours)
    target = memory_store.MemoryStore("gs", "ccx-r6t-audit-gcs")
    receipt = archive.archive_cloudtrail(
        _trail_bucket(trail), "AWSLogs/", target, run, (_PUBLIC_KEY,)
    )
    return trail, target, receipt


def _verify_aws(target, receipt, keys=(_PUBLIC_KEY,), run=_RUN) -> dict:
    return archive.verify_cloudtrail_archive(target, run, receipt, keys)


def _target_key(uri: str) -> str:
    return _OBJECTS + uri.removeprefix(f"s3://{fixture.BUCKET}/")


def _rewrite(target, key, mutate) -> str:
    """대상 버킷의 JSON 파일을 고쳐 쓰고, 새 바이트의 SHA-256 을 돌려준다."""
    document = json.loads(target.objects[key].data)
    mutate(document)
    data = json.dumps(document).encode()
    target.objects[key] = object_store.StoredObject(data=data)
    return hashlib.sha256(data).hexdigest()


def _forged_receipt(receipt, sha256, **changes) -> archive_files.Receipt:
    """운영자 기록까지 위조됐다고 가정할 때 — 안쪽 검사만 따로 시험한다."""
    return dataclasses.replace(receipt, archive_file_sha256=sha256, **changes)


def _missing(result: dict) -> list[str]:
    return [
        f["uri"]
        for f in result["cloudtrail"]["findings"]
        if f["status"] == "missing"
    ]


def test_cloudtrail_archive_writes_objects_bundle_and_receipt():
    trail, target, receipt = _archived()

    data = target.objects[_BUNDLE].data
    bundle = json.loads(data)

    assert bundle["format"] == archive_files.BUNDLE_FORMAT
    assert len(bundle["objects"]) == len(trail.objects)
    assert len(bundle["digest_signatures"]) == len(trail.digest_uris)
    for uri, data_ in trail.objects.items():
        assert target.objects[_target_key(uri)].data == data_
    assert receipt.archive_file == target.uri(_BUNDLE)
    assert receipt.archive_file_sha256 == hashlib.sha256(data).hexdigest()
    assert receipt.object_count == len(trail.objects)
    (bounds,) = receipt.chains.values()
    assert (bounds.oldest, bounds.newest) == (
        trail.digest_uris[0],
        trail.digest_uris[-1],
    )


def test_archived_cloudtrail_verifies_valid_to_the_chain_end():
    _, target, receipt = _archived()

    result = _verify_aws(target, receipt)

    assert result["result"] == "valid"
    assert result["archive_file"]["status"] == "valid"
    assert all(entry["status"] == "valid" for entry in result["transfer"])
    assert result["cloudtrail"]["result"] == "valid"


def test_one_byte_change_in_archive_is_invalid():
    trail, target, receipt = _archived()
    key = _target_key(trail.log_uris[1])
    tampered = bytearray(target.objects[key].data)
    tampered[-1] ^= 0x01
    target.objects[key] = object_store.StoredObject(data=bytes(tampered))

    result = _verify_aws(target, receipt)

    assert result["result"] == "invalid"
    statuses = {e["target"]: e["status"] for e in result["transfer"]}
    assert statuses[target.uri(key)] == "invalid"


def test_digest_deleted_from_archive_is_missing():
    trail, target, receipt = _archived()
    del target.objects[_target_key(trail.digest_uris[1])]

    result = _verify_aws(target, receipt)

    assert result["result"] == "invalid"
    assert _missing(result) == [trail.digest_uris[1]]


def test_bundle_changed_after_archive_is_invalid():
    _, target, receipt = _archived(1)
    _rewrite(target, _BUNDLE, lambda d: d["objects"].pop())

    result = _verify_aws(target, receipt)

    assert result["result"] == "invalid"
    assert "SHA-256" in result["archive_file"]["reason"]


def test_newest_digest_removed_with_its_bundle_rows_is_missing():
    trail, target, receipt = _archived()
    newest, newest_log = trail.digest_uris[-1], trail.log_uris[-1]
    for uri in (newest, newest_log):
        del target.objects[_target_key(uri)]

    def drop_newest(document):
        document["objects"] = [
            o
            for o in document["objects"]
            if o["source"] not in (newest, newest_log)
        ]
        document["digest_signatures"] = [
            s for s in document["digest_signatures"] if s["source"] != newest
        ]

    sha256 = _rewrite(target, _BUNDLE, drop_newest)
    forged = _forged_receipt(
        receipt, sha256, object_count=len(trail.objects) - 2
    )

    result = _verify_aws(target, forged)

    assert result["result"] == "invalid"
    assert newest in _missing(result)


def test_attacker_key_in_bundle_does_not_validate_forged_digest():
    trail, target, receipt = _archived(1)
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_der = attacker.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.PKCS1
    )
    digest_uri, log_uri = trail.digest_uris[0], trail.log_uris[0]
    forged_log = gzip.compress(b'{"Records": ["forged"]}')
    target.objects[_target_key(log_uri)] = object_store.StoredObject(
        data=forged_log
    )
    document = json.loads(gzip.decompress(trail.objects[digest_uri]))
    document["logFiles"][0]["hashValue"] = hashlib.sha256(
        gzip.decompress(forged_log)
    ).hexdigest()
    content = json.dumps(document).encode()
    forged_digest = gzip.compress(content)
    target.objects[_target_key(digest_uri)] = object_store.StoredObject(
        data=forged_digest
    )
    new_sha = {
        log_uri: hashlib.sha256(forged_log).hexdigest(),
        digest_uri: hashlib.sha256(forged_digest).hexdigest(),
    }

    def swap_key(bundle):
        for entry in bundle["objects"]:
            entry["sha256"] = new_sha.get(entry["source"], entry["sha256"])
        bundle["digest_signatures"][0]["signature"] = fixture.sign(
            document, content, attacker
        )
        bundle["public_keys"][0]["value"] = (
            archive_files.public_key_value_to_json(attacker_der)
        )

    forged = _forged_receipt(receipt, _rewrite(target, _BUNDLE, swap_key))

    result = _verify_aws(target, forged)

    assert result["result"] == "invalid"
    assert result["public_keys"]["status"] == "invalid"
    statuses = {f["uri"]: f["status"] for f in result["cloudtrail"]["findings"]}
    assert statuses[digest_uri] == "invalid"


def test_duplicate_source_rows_are_invalid():
    trail, target, receipt = _archived(1)

    def duplicate(document):
        row = dict(document["objects"][0])
        row["target"] = target.uri(_OBJECTS + "forged")
        document["objects"].append(row)

    forged = _forged_receipt(
        receipt,
        _rewrite(target, _BUNDLE, duplicate),
        object_count=len(trail.objects) + 1,
    )

    result = _verify_aws(target, forged)

    assert result["result"] == "invalid"
    assert "중복" in result["archive_file"]["reason"]


def test_bundle_of_another_run_is_invalid():
    _, target, other_receipt = _archived(1, run="other")
    data = target.objects["ccx-other/bundle.json"].data
    target.objects[_BUNDLE] = object_store.StoredObject(data=data)
    forged = dataclasses.replace(
        other_receipt, run_id=_RUN, archive_file=target.uri(_BUNDLE)
    )

    result = _verify_aws(target, forged)

    assert result["result"] == "invalid"
    assert "run" in result["archive_file"]["reason"]


def test_receipt_of_another_run_is_invalid():
    _, target, receipt = _archived(1)

    result = _verify_aws(target, receipt, run="other")

    assert result["result"] == "invalid"


def test_rows_outside_the_run_prefix_are_invalid():
    _, target, receipt = _archived(1)

    def point_elsewhere(document):
        document["objects"][0]["target"] = "gs://elsewhere/x"

    forged = _forged_receipt(
        receipt, _rewrite(target, _BUNDLE, point_elsewhere)
    )

    result = _verify_aws(target, forged)

    assert result["result"] == "invalid"
    assert "접두사" in result["archive_file"]["reason"]


def test_missing_bundle_is_invalid():
    _, target, receipt = _archived(1)
    del target.objects[_BUNDLE]

    result = _verify_aws(target, receipt)

    assert result["result"] == "invalid"


def test_empty_cloudtrail_archive_is_no_evidence():
    target = memory_store.MemoryStore("gs", "ccx-r6t-audit-gcs")
    receipt = archive.archive_cloudtrail(
        memory_store.MemoryStore("s3", "trail"),
        "AWSLogs/",
        target,
        _RUN,
        (_PUBLIC_KEY,),
    )

    result = _verify_aws(target, receipt)

    assert result["result"] == "no_evidence"


def test_second_archive_with_same_run_does_not_overwrite():
    trail = fixture.chain(1)
    target = memory_store.MemoryStore("gs", "ccx-r6t-audit-gcs")
    archive.archive_cloudtrail(
        _trail_bucket(trail), "AWSLogs/", target, _RUN, (_PUBLIC_KEY,)
    )

    with pytest.raises(object_store.ObjectExistsError):
        archive.archive_cloudtrail(
            _trail_bucket(trail), "AWSLogs/", target, _RUN, (_PUBLIC_KEY,)
        )


def _gcp_export() -> memory_store.MemoryStore:
    source = memory_store.MemoryStore("gs", "copy-out")
    source.objects[
        "cloudaudit.googleapis.com/activity/2026/11/07/00:00_00:59_S0.json"
    ] = object_store.StoredObject(data=b'[{"logName": "x"}]')
    return source


def _gcp_archived() -> tuple[memory_store.MemoryStore, archive_files.Receipt]:
    target = memory_store.MemoryStore("s3", "ccx-r6t-audit-s3")
    receipt = archive.archive_gcp_export(
        _gcp_export(), "cloudaudit.googleapis.com/", target, _RUN
    )
    return target, receipt


def test_gcp_export_archive_writes_manifest_with_no_evidence():
    target, receipt = _gcp_archived()

    manifest = json.loads(target.objects[_MANIFEST].data)

    assert manifest["evidence"] == "none"
    assert manifest["limitation"] == archive_files.GCP_LIMITATION
    assert len(manifest["objects"]) == 1
    assert receipt.direction == "gcp_to_aws"
    assert receipt.chains == {}


def test_archived_gcp_export_is_no_evidence_with_valid_transfer():
    target, receipt = _gcp_archived()

    result = archive.verify_gcp_archive(target, _RUN, receipt)

    assert result["result"] == "no_evidence"
    assert result["transfer_result"] == "valid"
    assert result["evidence"] == "none"
    assert result["limitation"] == archive_files.GCP_LIMITATION


def test_tampered_gcp_export_archive_is_invalid():
    target, receipt = _gcp_archived()
    (key,) = [k for k in target.objects if k.startswith(_OBJECTS)]
    target.objects[key] = object_store.StoredObject(data=b"[]")

    result = archive.verify_gcp_archive(target, _RUN, receipt)

    assert result["result"] == "invalid"


def test_gcp_object_and_manifest_changed_together_is_invalid():
    target, receipt = _gcp_archived()
    (key,) = [k for k in target.objects if k.startswith(_OBJECTS)]
    target.objects[key] = object_store.StoredObject(data=b"[]")

    def follow(document):
        document["objects"][0]["sha256"] = hashlib.sha256(b"[]").hexdigest()

    _rewrite(target, _MANIFEST, follow)

    result = archive.verify_gcp_archive(target, _RUN, receipt)

    assert result["result"] == "invalid"
    assert result["archive_file"]["status"] == "invalid"


def test_cloudtrail_receipt_cannot_verify_gcp_archive():
    target, _ = _gcp_archived()
    _, _, aws_receipt = _archived(1)

    result = archive.verify_gcp_archive(target, _RUN, aws_receipt)

    assert result["result"] == "invalid"


@pytest.mark.parametrize("run_id", ["", "UPPER", "a_b", "a-b", "x" * 17, "a\n"])
def test_invalid_run_id_is_rejected(run_id):
    with pytest.raises(ValueError):
        archive.archive_gcp_export(
            _gcp_export(),
            "",
            memory_store.MemoryStore("s3", "b"),
            run_id,
        )


def test_non_json_manifest_matching_receipt_is_invalid():
    target, receipt = _gcp_archived()
    target.objects[_MANIFEST] = object_store.StoredObject(data=b"not json")
    forged = _forged_receipt(receipt, hashlib.sha256(b"not json").hexdigest())

    result = archive.verify_gcp_archive(target, _RUN, forged)

    assert result["result"] == "invalid"


def test_bundle_with_fewer_rows_than_recorded_is_invalid():
    trail, target, receipt = _archived(2)
    dropped = trail.log_uris[0]

    def drop_log(document):
        document["objects"] = [
            o for o in document["objects"] if o["source"] != dropped
        ]

    forged = _forged_receipt(receipt, _rewrite(target, _BUNDLE, drop_log))

    result = _verify_aws(target, forged)

    assert result["result"] == "invalid"
    assert "객체 수" in result["archive_file"]["reason"]


def test_object_no_digest_covers_makes_archive_result_partial():
    trail = fixture.chain(2)
    source = _trail_bucket(trail)
    source.objects["AWSLogs/planted_FORGED.json.gz"] = (
        object_store.StoredObject(data=gzip.compress(b"{}"))
    )
    target = memory_store.MemoryStore("gs", "ccx-r6t-audit-gcs")
    receipt = archive.archive_cloudtrail(
        source, "AWSLogs/", target, _RUN, (_PUBLIC_KEY,)
    )

    result = _verify_aws(target, receipt)

    assert result["result"] == "partial"
    assert result["cloudtrail"]["counts"]["evidence_none"] == 1
