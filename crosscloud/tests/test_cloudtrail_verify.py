import gzip
import json

import cloudtrail_fixture as fixture
import pytest

from crosscloud.audit_copy import cloudtrail_verify


def _verify(
    trail: fixture.FakeTrail,
    *,
    public_keys: dict[str, bytes] | None = None,
) -> cloudtrail_verify.VerificationReport:
    if public_keys is None:
        public_keys = {fixture.FINGERPRINT: fixture.public_key_der()}
    return cloudtrail_verify.verify_cloudtrail(
        object_uris=(*trail.digest_uris, *trail.log_uris),
        signatures={
            uri: cloudtrail_verify.DigestSignature(
                signature=signature, algorithm="SHA256withRSA"
            )
            for uri, signature in trail.signatures.items()
        },
        public_keys=public_keys,
        read=trail.objects.get,
    )


def _status(
    report: cloudtrail_verify.VerificationReport, uri: str
) -> cloudtrail_verify.Finding:
    (finding,) = [f for f in report.findings if f.uri == uri]
    return finding


def _tamper_log_content(trail: fixture.FakeTrail, uri: str) -> None:
    content = bytearray(gzip.decompress(trail.objects[uri]))
    content[0] ^= 0x01
    trail.objects[uri] = gzip.compress(bytes(content))


def test_intact_chain_is_valid_to_the_end():
    trail = fixture.chain(3)

    report = _verify(trail)

    assert report.ok
    assert report.evidence_none_spans == ()
    statuses = {f.uri: f.status for f in report.findings}
    assert statuses == {uri: "valid" for uri in trail.objects}


def test_one_byte_change_in_log_content_is_invalid():
    trail = fixture.chain(3)
    tampered = trail.log_uris[1]
    _tamper_log_content(trail, tampered)

    report = _verify(trail)

    assert not report.ok
    finding = _status(report, tampered)
    assert finding.status == "invalid"
    assert "hash" in finding.reason


def test_one_byte_change_in_raw_log_bytes_is_invalid():
    trail = fixture.chain(2)
    tampered = trail.log_uris[0]
    raw = bytearray(trail.objects[tampered])
    raw[len(raw) // 2] ^= 0x01
    trail.objects[tampered] = bytes(raw)

    report = _verify(trail)

    assert _status(report, tampered).status == "invalid"


def test_one_byte_change_in_digest_breaks_its_signature():
    trail = fixture.chain(3)
    tampered = trail.digest_uris[1]
    content = gzip.decompress(trail.objects[tampered])
    content = content.replace(b'"awsAccountId": "1', b'"awsAccountId": "2')
    trail.objects[tampered] = gzip.compress(content)

    report = _verify(trail)

    finding = _status(report, tampered)
    assert finding.status == "invalid"
    assert "signature" in finding.reason
    assert _status(report, trail.digest_uris[0]).status == "valid"
    assert _status(report, trail.digest_uris[2]).status == "valid"


def test_deleted_digest_is_reported_missing():
    trail = fixture.chain(3)
    deleted = trail.digest_uris[1]
    del trail.objects[deleted]

    report = _verify(trail)

    assert not report.ok
    assert _status(report, deleted).status == "missing"
    assert _status(report, trail.digest_uris[0]).status == "valid"
    assert _status(report, trail.digest_uris[2]).status == "valid"


def test_digest_deleted_from_archive_and_bundle_is_found_by_chain_link():
    trail = fixture.chain(3)
    deleted = trail.digest_uris[1]
    del trail.objects[deleted]
    del trail.signatures[deleted]
    trail.digest_uris.remove(deleted)

    report = _verify(trail)

    assert not report.ok
    assert _status(report, deleted).status == "missing"


def test_logs_of_a_missing_digest_have_no_evidence():
    trail = fixture.chain(3)
    del trail.objects[trail.digest_uris[1]]

    report = _verify(trail)

    finding = _status(report, trail.log_uris[1])
    assert finding.status == "evidence_none"


def test_deleted_log_file_is_reported_missing():
    trail = fixture.chain(2)
    deleted = trail.log_uris[0]
    del trail.objects[deleted]

    report = _verify(trail)

    assert not report.ok
    assert _status(report, deleted).status == "missing"


def test_validation_off_period_is_an_evidence_none_span():
    trail = fixture.FakeTrail()
    trail.add_hour(0)
    trail.add_hour(1, validation_on=False)
    trail.add_hour(2, validation_on=False)
    trail.add_hour(3)

    report = _verify(trail)

    assert report.ok
    (span,) = report.evidence_none_spans
    assert (span.start, span.end) == (
        "2026-11-07T01:00:00Z",
        "2026-11-07T03:00:00Z",
    )
    assert _status(report, trail.log_uris[1]).status == "evidence_none"
    assert _status(report, trail.log_uris[2]).status == "evidence_none"
    assert _status(report, trail.log_uris[3]).status == "valid"


def test_backfill_digest_is_not_verified_and_has_no_evidence():
    trail = fixture.chain(2)
    backfill = trail.add_backfill_digest(5)

    report = _verify(trail)

    assert report.ok
    finding = _status(report, backfill)
    assert finding.status == "evidence_none"
    assert "backfill" in finding.reason


def test_digest_at_a_different_original_path_is_invalid_as_moved():
    trail = fixture.chain(2)
    original = trail.digest_uris[1]
    moved = original.replace("trail-bucket", "other-bucket")
    trail.objects[moved] = trail.objects.pop(original)
    trail.signatures[moved] = trail.signatures.pop(original)
    trail.digest_uris[1] = moved

    report = _verify(trail)

    finding = _status(report, moved)
    assert finding.status == "invalid"
    assert "path" in finding.reason


def test_digest_without_saved_signature_is_invalid():
    trail = fixture.chain(2)
    del trail.signatures[trail.digest_uris[1]]

    report = _verify(trail)

    finding = _status(report, trail.digest_uris[1])
    assert finding.status == "invalid"
    assert "signature" in finding.reason


def test_unknown_public_key_fingerprint_is_invalid():
    trail = fixture.chain(1)

    report = _verify(trail, public_keys={})

    finding = _status(report, trail.digest_uris[0])
    assert finding.status == "invalid"
    assert "public key" in finding.reason


def test_digest_that_is_not_gzip_json_is_invalid():
    trail = fixture.chain(1)
    trail.objects[trail.digest_uris[0]] = b"not gzip"

    report = _verify(trail)

    assert _status(report, trail.digest_uris[0]).status == "invalid"


def test_unsupported_signature_algorithm_is_invalid():
    trail = fixture.chain(1)
    uri = trail.digest_uris[0]

    report = cloudtrail_verify.verify_cloudtrail(
        object_uris=tuple(trail.objects),
        signatures={
            uri: cloudtrail_verify.DigestSignature(
                signature=trail.signatures[uri], algorithm="SHA1withRSA"
            )
        },
        public_keys={fixture.FINGERPRINT: fixture.public_key_der()},
        read=trail.objects.get,
    )

    assert _status(report, uri).status == "invalid"


def test_digest_with_missing_required_field_is_invalid():
    trail = fixture.chain(1)
    uri = trail.digest_uris[0]
    document = json.loads(gzip.decompress(trail.objects[uri]))
    del document["digestEndTime"]
    trail.objects[uri] = gzip.compress(json.dumps(document).encode())

    report = _verify(trail)

    assert _status(report, uri).status == "invalid"


def test_report_dict_marks_evidence_none_explicitly():
    trail = fixture.FakeTrail()
    trail.add_hour(0)
    trail.add_hour(1, validation_on=False)
    trail.add_hour(2)

    document = cloudtrail_verify.report_to_dict(_verify(trail))

    assert document["result"] == "valid"
    (span,) = document["evidence_none_spans"]
    assert span["evidence"] == "none"
    no_evidence = [
        f for f in document["findings"] if f["status"] == "evidence_none"
    ]
    assert no_evidence
    assert all(f["evidence"] == "none" for f in no_evidence)


@pytest.mark.parametrize("uri", ["gs://bucket/key", "s3://bucket-only"])
def test_non_s3_object_uri_is_rejected(uri):
    with pytest.raises(ValueError):
        cloudtrail_verify.verify_cloudtrail(
            object_uris=(uri,),
            signatures={},
            public_keys={},
            read=lambda _: None,
        )
