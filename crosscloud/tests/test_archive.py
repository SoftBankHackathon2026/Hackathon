import json

import cloudtrail_fixture as fixture
import memory_store
import pytest

from crosscloud.audit_copy import archive, archive_files, object_store

_PUBLIC_KEY = archive_files.PublicKey(
    fingerprint=fixture.FINGERPRINT,
    value=fixture.public_key_der(),
    validity_start="2026-11-01T00:00:00Z",
    validity_end="2026-12-01T00:00:00Z",
)
_RUN = "r6t"
_OBJECTS = f"ccx-{_RUN}/objects/"


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
    hours: int = 3,
) -> tuple[fixture.FakeTrail, memory_store.MemoryStore]:
    trail = fixture.chain(hours)
    target = memory_store.MemoryStore("gs", "ccx-r6t-audit")
    archive.archive_cloudtrail(
        _trail_bucket(trail), "AWSLogs/", target, _RUN, (_PUBLIC_KEY,)
    )
    return trail, target


def _target_key(uri: str) -> str:
    return _OBJECTS + uri.removeprefix(f"s3://{fixture.BUCKET}/")


def test_cloudtrail_archive_writes_objects_and_bundle():
    trail, target = _archived()

    bundle = json.loads(target.objects[f"ccx-{_RUN}/bundle.json"].data)

    assert bundle["format"] == archive_files.BUNDLE_FORMAT
    assert len(bundle["objects"]) == len(trail.objects)
    assert len(bundle["digest_signatures"]) == len(trail.digest_uris)
    for uri, data in trail.objects.items():
        assert target.objects[_target_key(uri)].data == data


def test_archived_cloudtrail_verifies_valid_to_the_chain_end():
    _, target = _archived()

    result = archive.verify_cloudtrail_archive(target, _RUN)

    assert result["result"] == "valid"
    assert all(entry["status"] == "valid" for entry in result["transfer"])
    assert result["cloudtrail"]["result"] == "valid"


def test_one_byte_change_in_archive_is_invalid():
    trail, target = _archived()
    key = _target_key(trail.log_uris[1])
    stored = target.objects[key]
    tampered = bytearray(stored.data)
    tampered[-1] ^= 0x01
    target.objects[key] = object_store.StoredObject(data=bytes(tampered))

    result = archive.verify_cloudtrail_archive(target, _RUN)

    assert result["result"] == "invalid"
    statuses = {e["target"]: e["status"] for e in result["transfer"]}
    assert statuses[target.uri(key)] == "invalid"


def test_digest_deleted_from_archive_is_missing():
    trail, target = _archived()
    del target.objects[_target_key(trail.digest_uris[1])]

    result = archive.verify_cloudtrail_archive(target, _RUN)

    assert result["result"] == "invalid"
    missing = [
        f["uri"]
        for f in result["cloudtrail"]["findings"]
        if f["status"] == "missing"
    ]
    assert missing == [trail.digest_uris[1]]


def test_bundle_pointing_outside_the_archive_bucket_is_invalid():
    _, target = _archived(1)
    key = f"ccx-{_RUN}/bundle.json"
    document = json.loads(target.objects[key].data)
    document["objects"][0]["target"] = "gs://elsewhere/x"
    target.objects[key] = object_store.StoredObject(
        data=json.dumps(document).encode()
    )

    result = archive.verify_cloudtrail_archive(target, _RUN)

    assert result["result"] == "invalid"
    assert result["transfer"][0]["status"] == "missing"


def test_verify_without_bundle_raises():
    target = memory_store.MemoryStore("gs", "empty")

    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive.verify_cloudtrail_archive(target, _RUN)


def test_second_archive_with_same_run_does_not_overwrite():
    trail = fixture.chain(1)
    target = memory_store.MemoryStore("gs", "ccx-r6t-audit")
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


def test_gcp_export_archive_writes_manifest_with_no_evidence():
    target = memory_store.MemoryStore("s3", "ccx-r6t-audit")

    manifest = archive.archive_gcp_export(
        _gcp_export(), "cloudaudit.googleapis.com/", target, _RUN
    )

    stored = json.loads(target.objects[f"ccx-{_RUN}/manifest.json"].data)
    assert stored == manifest
    assert manifest["evidence"] == "none"
    assert manifest["limitation"] == archive_files.GCP_LIMITATION
    assert len(manifest["objects"]) == 1


def test_archived_gcp_export_verifies_transfer_only():
    target = memory_store.MemoryStore("s3", "ccx-r6t-audit")
    archive.archive_gcp_export(
        _gcp_export(), "cloudaudit.googleapis.com/", target, _RUN
    )

    result = archive.verify_gcp_archive(target, _RUN)

    assert result["result"] == "valid"
    assert result["evidence"] == "none"
    assert result["limitation"] == archive_files.GCP_LIMITATION


def test_tampered_gcp_export_archive_is_invalid():
    target = memory_store.MemoryStore("s3", "ccx-r6t-audit")
    archive.archive_gcp_export(
        _gcp_export(), "cloudaudit.googleapis.com/", target, _RUN
    )
    (key,) = [k for k in target.objects if k.startswith(_OBJECTS)]
    target.objects[key] = object_store.StoredObject(data=b"[]")

    result = archive.verify_gcp_archive(target, _RUN)

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


def test_verify_with_non_json_manifest_raises():
    target = memory_store.MemoryStore("s3", "b")
    target.objects[f"ccx-{_RUN}/manifest.json"] = object_store.StoredObject(
        data=b"not json"
    )

    with pytest.raises(archive_files.InvalidArchiveFileError):
        archive.verify_gcp_archive(target, _RUN)
