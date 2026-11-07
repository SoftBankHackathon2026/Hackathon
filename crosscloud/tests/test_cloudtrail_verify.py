import base64
import gzip
import json

import cloudtrail_fixture as fixture
import pytest
from cryptography.hazmat.primitives import serialization

from crosscloud.audit_copy import cloudtrail_verify


def _verify(
    trail: fixture.FakeTrail,
    *,
    public_keys: dict[str, bytes] | None = None,
    recorded: list[str] | None = None,
) -> cloudtrail_verify.VerificationReport:
    if public_keys is None:
        public_keys = {fixture.FINGERPRINT: fixture.public_key_der()}
    if recorded is None:
        recorded = trail.recorded_digest_uris
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
        chain_bounds=cloudtrail_verify.chain_bounds(recorded),
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

    assert report.result == "valid"
    assert report.evidence_none_spans == ()
    statuses = {f.uri: f.status for f in report.findings}
    assert statuses == {uri: "valid" for uri in trail.objects}


def test_one_byte_change_in_log_content_is_invalid():
    trail = fixture.chain(3)
    tampered = trail.log_uris[1]
    _tamper_log_content(trail, tampered)

    report = _verify(trail)

    assert report.result == "invalid"
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

    assert report.result == "invalid"
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

    assert report.result == "invalid"
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

    assert report.result == "invalid"
    assert _status(report, deleted).status == "missing"


def test_validation_off_period_is_an_evidence_none_span():
    trail = fixture.FakeTrail()
    trail.add_hour(0)
    trail.add_hour(1, validation_on=False)
    trail.add_hour(2, validation_on=False)
    trail.add_hour(3)

    report = _verify(trail)

    # 증거 없는 구간이 있으면 전체 valid 로 올리지 않는다.
    assert report.result == "partial"
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

    assert report.result == "partial"
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
        chain_bounds=cloudtrail_verify.chain_bounds(trail.digest_uris),
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

    assert document["result"] == "partial"
    assert document["counts"] == {
        "valid": 4,
        "evidence_none": 1,
        "invalid": 0,
        "missing": 0,
    }
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
            chain_bounds={},
        )


def _forget(trail: fixture.FakeTrail, index: int) -> str:
    """이관 기록은 그대로 두고, 다이제스트 하나와 그 로그를 bundle·아카이브
    양쪽에서 지운다(경계 삭제 위조)."""
    digest = trail.digest_uris.pop(index)
    log = trail.log_uris.pop(index)
    for uri in (digest, log):
        del trail.objects[uri]
    del trail.signatures[digest]
    return digest


def test_newest_digest_deleted_with_its_rows_is_missing():
    trail = fixture.chain(3)
    deleted = _forget(trail, 2)

    report = _verify(trail)

    assert report.result == "invalid"
    assert _status(report, deleted).status == "missing"


def test_oldest_digest_deleted_with_its_rows_is_missing():
    trail = fixture.chain(3)
    deleted = _forget(trail, 0)

    report = _verify(trail)

    assert report.result == "invalid"
    assert _status(report, deleted).status == "missing"


def test_recorded_range_start_with_previous_link_is_accepted():
    # 복사 범위가 체인 중간에서 시작했다: 가장 오래된 다이제스트의 이전
    # 링크가 범위 밖을 가리키지만, 이관 기록의 체인 시작과 같으므로 정상.
    trail = fixture.chain(3)
    _forget(trail, 0)

    report = _verify(trail, recorded=list(trail.digest_uris))

    assert report.result == "valid"


def test_leftover_previous_link_without_record_is_missing():
    trail = fixture.chain(3)
    _forget(trail, 0)

    report = _verify(trail, recorded=[])

    assert report.result == "invalid"


def test_previous_signature_mismatch_in_link_is_invalid():
    trail = fixture.FakeTrail()
    trail.add_hour(0)
    trail.forged_previous_signature = "00" * 256
    trail.add_hour(1)

    report = _verify(trail)

    assert report.result == "invalid"
    finding = _status(report, trail.digest_uris[0])
    assert finding.status == "invalid"
    assert "link" in finding.reason


def test_previous_bucket_mismatch_in_link_is_invalid():
    trail = fixture.FakeTrail()
    trail.add_hour(0)
    trail.forged_previous_bucket = "other-bucket"
    trail.add_hour(1)

    report = _verify(trail)

    assert _status(report, trail.digest_uris[0]).status == "invalid"


def test_logs_without_any_digest_are_no_evidence_not_valid():
    trail = fixture.FakeTrail()
    trail.add_hour(0, validation_on=False)
    trail.add_hour(1, validation_on=False)

    report = _verify(trail)

    assert report.result == "no_evidence"
    assert cloudtrail_verify.report_to_dict(report)["result"] == "no_evidence"


def test_empty_archive_is_no_evidence():
    report = _verify(fixture.FakeTrail())

    assert report.result == "no_evidence"


def test_chain_bounds_record_oldest_and_newest_per_chain():
    trail = fixture.chain(3)
    trail.add_backfill_digest(5)

    bounds = cloudtrail_verify.chain_bounds(trail.recorded_digest_uris)

    (only,) = bounds.values()
    assert only == cloudtrail_verify.ChainBounds(
        oldest=trail.digest_uris[0], newest=trail.digest_uris[2]
    )


def test_empty_previous_key_starts_a_new_chain_like_null():
    trail = fixture.FakeTrail()
    trail.add_hour(0)
    trail.forged_previous_key = ""
    trail.add_hour(1)

    report = _verify(trail)

    assert report.result == "partial"
    assert len(report.evidence_none_spans) == 1


def _resign(trail: fixture.FakeTrail, uri: str, change) -> None:
    """다이제스트 본문을 바꾸고 시험용 키로 다시 서명한다(형식 오류 시험)."""
    document = json.loads(gzip.decompress(trail.objects[uri]))
    change(document)
    content = json.dumps(document).encode()
    trail.objects[uri] = gzip.compress(content)
    trail.signatures[uri] = fixture.sign(document, content)


@pytest.mark.parametrize(
    "log_files", ["not a list", [{"s3Bucket": fixture.BUCKET}], ["x"]]
)
def test_malformed_log_files_in_signed_digest_is_invalid(log_files):
    trail = fixture.chain(1)
    uri = trail.digest_uris[0]
    _resign(trail, uri, lambda d: d.update(logFiles=log_files))

    report = _verify(trail)

    finding = _status(report, uri)
    assert finding.status == "invalid"
    assert "logFiles" in finding.reason


def test_non_hex_saved_signature_is_invalid():
    trail = fixture.chain(1)
    uri = trail.digest_uris[0]
    trail.signatures[uri] = "zz"

    report = _verify(trail)

    assert _status(report, uri).status == "invalid"


def test_recorded_chain_absent_from_archive_is_missing():
    trail = fixture.chain(1)
    deleted = trail.digest_uris.pop()
    del trail.objects[deleted]
    del trail.signatures[deleted]

    report = _verify(trail)

    assert report.result == "invalid"
    assert _status(report, deleted).status == "missing"


def test_planted_log_no_digest_covers_makes_result_partial():
    # trail 버킷에 끼워 넣은 객체: 어떤 다이제스트도 가리키지 않는다.
    trail = fixture.chain(2)
    planted = trail.log_uris[0].replace("_abc.json.gz", "_FORGED.json.gz")
    trail.objects[planted] = gzip.compress(b'{"Records": []}')
    trail.log_uris.append(planted)

    report = _verify(trail)

    assert report.result == "partial"
    assert _status(report, planted).status == "evidence_none"


def test_newest_log_not_yet_covered_by_a_digest_makes_result_partial():
    trail = fixture.chain(2)
    trail.add_hour(2, validation_on=False)

    report = _verify(trail)

    assert report.result == "partial"


@pytest.mark.parametrize("algorithm", ["MD5", None])
def test_log_entry_with_non_sha256_hash_algorithm_is_invalid(algorithm):
    # 공식 절차는 logFiles.hashAlgorithm 에 적힌 알고리즘으로 해시하라고 한다.
    # SHA-256 밖의 값을 SHA-256 으로 가정해 통과시키지 않는다.
    trail = fixture.chain(1)
    uri = trail.digest_uris[0]
    log_uri = trail.log_uris[0]

    def change(document):
        entry = document["logFiles"][0]
        if algorithm is None:
            del entry["hashAlgorithm"]
        else:
            entry["hashAlgorithm"] = algorithm

    _resign(trail, uri, change)

    report = _verify(trail)

    assert report.result == "invalid"
    finding = _status(report, log_uri)
    assert finding.status == "invalid"
    assert "hashAlgorithm" in finding.reason


def test_subject_public_key_info_der_key_is_accepted():
    # AWS 문서의 list-public-keys 샘플은 PKCS#1 과
    # SPKI(MIIBIjANBgkqhkiG9w0BAQEF…)가 섞여 있고 인코딩이 바뀔 수 있다고
    # 적는다. 두 형식 모두 읽어야 한다(aws-cli load_pkcs1 은 SPKI 를 못 읽음).
    spki = (
        fixture.private_key()
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    assert base64.b64encode(spki).startswith(b"MIIBIjANBgkqhkiG9w0BAQEF")
    trail = fixture.chain(2)

    report = _verify(trail, public_keys={fixture.FINGERPRINT: spki})

    assert report.result == "valid"


@pytest.mark.parametrize(
    "field", ["previousDigestS3Object", "previousDigestS3Bucket"]
)
@pytest.mark.parametrize("hours", [1, 2])
def test_digest_without_previous_link_field_is_invalid(field, hours):
    trail = fixture.chain(hours)
    newest = trail.digest_uris[-1]
    _resign(trail, newest, lambda d: d.pop(field))

    report = _verify(trail)

    assert report.result == "invalid"
    finding = _status(report, newest)
    assert finding.status == "invalid"
    assert "필드" in finding.reason
