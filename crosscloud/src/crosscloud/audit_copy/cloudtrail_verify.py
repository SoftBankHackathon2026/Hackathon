"""옮긴 CloudTrail 로그·다이제스트의 오프라인 무결성 검증기.

aws-cli `validate-logs`(awscli/customizations/cloudtrail/validation.py)를
참조한다. 다른 점은 셋이다(RC-A21 오프라인 3요건).

*   서명은 S3 메타데이터에서 읽지 않고, 이관 전에 받아 둔 값(bundle)을 쓴다.
*   공개키는 ListPublicKeys 결과를 입력으로 받는다(지문 → PKCS#1 DER).
*   "원래 위치에 있는가" 검사는 경로 대응표 검사로 바꾼다. 객체는 원래
    S3 경로(s3://버킷/키)로 부르고, 새 위치에서 읽는 일은 호출자의 read
    함수가 맡는다. 다이제스트 본문의 경로가 그 원래 경로와 같아야 한다.

backfill 체인은 검증하지 않고, 검증이 꺼진 구간·체인 공백과 함께 "증거
없음"(evidence: none)으로 표시한다. 무결성 확인으로 올리지 않는다.

  report = cloudtrail_verify.verify_cloudtrail(
      object_uris=uris, signatures=sigs, public_keys=keys, read=read)
  if not report.ok: ...
"""

import collections
import dataclasses
import datetime
import gzip
import hashlib
import json
import re
import zlib
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from cryptography import exceptions as crypto_exceptions
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

Status = Literal["valid", "invalid", "missing", "evidence_none"]
ObjectKind = Literal["digest", "log"]

SIGNATURE_ALGORITHM = "SHA256withRSA"

# aws-cli DigestTraverser.required_digest_keys 와 같은 목록.
_REQUIRED_DIGEST_FIELDS = (
    "digestPublicKeyFingerprint",
    "digestS3Bucket",
    "digestS3Object",
    "previousDigestSignature",
    "digestEndTime",
    "digestStartTime",
)
# AWSLogs/<계정>/CloudTrail-Digest/<리전>/YYYY/MM/DD/<이름>_<끝 시각>.json.gz
# (backfill 은 끝 시각 뒤에 _backfill). 체인은 날짜 디렉토리와 끝 시각을 뺀
# 나머지가 같은 다이제스트들이다.
_DIGEST_KEY = re.compile(
    r"^(?P<base>.*/CloudTrail-Digest/[^/]+)/\d{4}/\d{2}/\d{2}/"
    r"(?P<stem>.+)_(?P<end>\d{8}T\d{6}Z)(?P<backfill>_backfill)?\.json\.gz$"
)
_EVIDENCE_NONE = "none"


@dataclasses.dataclass(frozen=True)
class DigestSignature:
    """다이제스트 객체의 S3 메타데이터(서명). 이관 전 원위치에서 받아 둔다.

    Attributes:
        signature: x-amz-meta-signature 값(16진 문자열).
        algorithm: x-amz-meta-signature-algorithm 값.
    """

    signature: str
    algorithm: str


@dataclasses.dataclass(frozen=True)
class Finding:
    """객체 하나의 판정. uri 는 원래 S3 경로다."""

    uri: str
    kind: ObjectKind
    status: Status
    reason: str


@dataclasses.dataclass(frozen=True)
class EvidenceNoneSpan:
    """공급자 증거가 없는 시간 구간(검증 꺼짐·로깅 중단·체인 공백)."""

    chain: str
    start: str
    end: str
    reason: str


@dataclasses.dataclass(frozen=True)
class VerificationReport:
    """검증 결과. invalid·missing 이 하나라도 있으면 ok 가 아니다.

    evidence_none 은 실패가 아니지만 무결성 확인도 아니다.
    """

    findings: tuple[Finding, ...]
    evidence_none_spans: tuple[EvidenceNoneSpan, ...]

    @property
    def ok(self) -> bool:
        """invalid·missing 판정이 하나도 없으면 True."""
        return all(
            f.status in ("valid", "evidence_none") for f in self.findings
        )


class _DigestError(Exception):
    """다이제스트 하나가 무효다. 메시지가 판정 사유가 된다."""


@dataclasses.dataclass(frozen=True)
class _DigestKey:
    uri: str
    chain: str
    end: str
    is_backfill: bool


def verify_cloudtrail(
    object_uris: Iterable[str],
    signatures: Mapping[str, DigestSignature],
    public_keys: Mapping[str, bytes],
    read: Callable[[str], bytes | None],
) -> VerificationReport:
    """옮긴 CloudTrail 객체들을 다이제스트 체인으로 검증한다.

    Args:
        object_uris: 옮긴 모든 객체의 원래 경로(s3://버킷/키). 로그·다이제스트.
        signatures: 다이제스트 원래 경로 → 이관 전에 받아 둔 서명.
        public_keys: 공개키 지문 → PKCS#1 DER 바이트(ListPublicKeys Value).
        read: 원래 경로 → 새 위치의 원본 바이트. 없으면 None.

    Returns:
        객체별 판정과 증거 없음 구간. object_uris 의 모든 객체와, 다이제스트가
        가리키지만 object_uris 에 없는 로그·다이제스트가 판정에 들어간다.

    Raises:
        ValueError: object_uris 에 s3://버킷/키 형식이 아닌 값이 있을 때.
    """
    uris = tuple(object_uris)
    for uri in uris:
        _split_s3_uri(uri)
    digests = [_parse_digest_key(uri) for uri in uris]
    digest_keys = [d for d in digests if d is not None]
    findings: list[Finding] = []
    spans: list[EvidenceNoneSpan] = []
    covered_logs: set[str] = set()
    chains = collections.defaultdict(list)
    for digest_key in digest_keys:
        if digest_key.is_backfill:
            findings.append(
                _no_evidence(
                    digest_key.uri, "digest", "backfill 체인은 검증 안 함"
                )
            )
        else:
            chains[digest_key.chain].append(digest_key)
    verifier = _ChainVerifier(signatures, public_keys, read)
    for chain, keys in sorted(chains.items()):
        result = verifier.verify_chain(chain, keys)
        findings.extend(result.findings)
        spans.extend(result.evidence_none_spans)
        covered_logs.update(f.uri for f in result.findings if f.kind == "log")
    digest_uris = {d.uri for d in digest_keys}
    for uri in uris:
        if uri not in digest_uris and uri not in covered_logs:
            findings.append(
                _no_evidence(uri, "log", "검증된 다이제스트가 가리키지 않음")
            )
    return VerificationReport(tuple(findings), tuple(spans))


def report_to_dict(report: VerificationReport) -> dict[str, Any]:
    """검증 결과를 JSON 으로 쓸 dict 로 바꾼다. 증거 없음은 evidence: none."""
    findings = []
    for finding in report.findings:
        entry = dataclasses.asdict(finding)
        if finding.status == "evidence_none":
            entry["evidence"] = _EVIDENCE_NONE
        findings.append(entry)
    spans = [
        {**dataclasses.asdict(span), "evidence": _EVIDENCE_NONE}
        for span in report.evidence_none_spans
    ]
    return {
        "result": "valid" if report.ok else "invalid",
        "findings": findings,
        "evidence_none_spans": spans,
    }


class _ChainVerifier:
    """한 체인을 최신 다이제스트부터 이전 링크를 따라 거슬러 검증한다."""

    def __init__(
        self,
        signatures: Mapping[str, DigestSignature],
        public_keys: Mapping[str, bytes],
        read: Callable[[str], bytes | None],
    ) -> None:
        self._signatures = signatures
        self._public_keys = public_keys
        self._read = read

    def verify_chain(
        self, chain: str, keys: list[_DigestKey]
    ) -> VerificationReport:
        findings: list[Finding] = []
        spans: list[EvidenceNoneSpan] = []
        newer: dict[str, Any] | None = None
        for key in sorted(keys, key=lambda k: k.end, reverse=True):
            if newer is not None:
                findings.extend(self._check_link(newer, key, chain, spans))
            raw = self._read(key.uri)
            if raw is None:
                findings.append(Finding(key.uri, "digest", "missing", "없음"))
                newer = None
                continue
            try:
                document = self._verify_digest(key.uri, raw)
            except _DigestError as e:
                findings.append(Finding(key.uri, "digest", "invalid", str(e)))
                newer = None
                continue
            findings.append(Finding(key.uri, "digest", "valid", "서명 일치"))
            findings.extend(self._verify_logs(document))
            newer = document
        return VerificationReport(tuple(findings), tuple(spans))

    def _check_link(
        self,
        newer: dict[str, Any],
        older: _DigestKey,
        chain: str,
        spans: list[EvidenceNoneSpan],
    ) -> list[Finding]:
        """새 다이제스트의 이전 링크가 바로 다음 다이제스트를 가리키는지 본다.

        링크가 null 이면 체인이 거기서 새로 시작했다(검증 꺼짐·로깅 중단) —
        그 사이는 증거 없음 구간이다. 링크가 다른 키를 가리키면 그 다이제스트가
        사라진 것이다(아카이브와 bundle 양쪽에서 지워진 경우).
        """
        previous_key = newer["previousDigestS3Object"]
        if previous_key is None:
            spans.append(
                EvidenceNoneSpan(
                    chain=chain,
                    start=_end_time_of(older),
                    end=newer["digestStartTime"],
                    reason="체인이 새로 시작함(검증 꺼짐 또는 로깅 중단)",
                )
            )
            return []
        _, older_key = _split_s3_uri(older.uri)
        if previous_key == older_key:
            return []
        previous_uri = f"s3://{newer['previousDigestS3Bucket']}/{previous_key}"
        return [
            Finding(previous_uri, "digest", "missing", "체인 링크가 가리킴")
        ]

    def _verify_digest(self, uri: str, raw: bytes) -> dict[str, Any]:
        try:
            content = gzip.decompress(raw)
            document = json.loads(content)
        except (OSError, EOFError, zlib.error, ValueError) as e:
            raise _DigestError("gzip JSON 이 아님") from e
        if not isinstance(document, dict) or any(
            field not in document for field in _REQUIRED_DIGEST_FIELDS
        ):
            raise _DigestError("필수 필드 없음")
        recorded = (
            f"s3://{document['digestS3Bucket']}/{document['digestS3Object']}"
        )
        if recorded != uri:
            raise _DigestError("본문의 원래 path 와 대응표의 원래 경로가 다름")
        signature = self._signatures.get(uri)
        if signature is None:
            raise _DigestError("받아 둔 signature 메타데이터 없음")
        if signature.algorithm != SIGNATURE_ALGORITHM:
            raise _DigestError(
                f"지원하지 않는 signature 알고리즘: {signature.algorithm}"
            )
        public_key = self._public_key(document["digestPublicKeyFingerprint"])
        try:
            public_key.verify(
                bytes.fromhex(signature.signature),
                _string_to_sign(document, content),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (crypto_exceptions.InvalidSignature, ValueError) as e:
            raise _DigestError("signature 불일치") from e
        return document

    def _public_key(self, fingerprint: str) -> rsa.RSAPublicKey:
        der = self._public_keys.get(fingerprint)
        if der is None:
            raise _DigestError(f"public key 없음(지문 {fingerprint})")
        try:
            key = serialization.load_der_public_key(der)
        except ValueError as e:
            raise _DigestError("public key 형식 오류") from e
        if not isinstance(key, rsa.RSAPublicKey):
            raise _DigestError("public key 가 RSA 가 아님")
        return key

    def _verify_logs(self, document: dict[str, Any]) -> list[Finding]:
        findings = []
        for log in document.get("logFiles") or ():
            uri = f"s3://{log['s3Bucket']}/{log['s3Object']}"
            raw = self._read(uri)
            if raw is None:
                findings.append(Finding(uri, "log", "missing", "없음"))
                continue
            findings.append(_verify_log(uri, raw, log["hashValue"]))
        return findings


def _verify_log(uri: str, raw: bytes, expected_hash: str) -> Finding:
    """로그 해시는 압축 해제한 내용 기준이다(RC-A20).

    gzip 스트림 뒤에 남는 바이트가 있으면 aws-cli 처럼 무효로 본다.
    """
    inflater = zlib.decompressobj(zlib.MAX_WBITS | 16)
    try:
        content = inflater.decompress(raw) + inflater.flush()
    except zlib.error:
        return Finding(uri, "log", "invalid", "gzip 형식 오류")
    if not inflater.eof or inflater.unused_data:
        return Finding(uri, "log", "invalid", "gzip 스트림 끝이 맞지 않음")
    if hashlib.sha256(content).hexdigest() != expected_hash:
        return Finding(uri, "log", "invalid", "hash 불일치")
    return Finding(uri, "log", "valid", "hash 일치")


def _string_to_sign(document: dict[str, Any], content: bytes) -> bytes:
    """aws-cli Sha256RSADigestValidator._create_string_to_sign 과 같다."""
    previous = document["previousDigestSignature"] or "null"
    return (
        f"{document['digestEndTime']}\n"
        f"{document['digestS3Bucket']}/{document['digestS3Object']}\n"
        f"{hashlib.sha256(content).hexdigest()}\n"
        f"{previous}"
    ).encode()


def _parse_digest_key(uri: str) -> _DigestKey | None:
    _, key = _split_s3_uri(uri)
    match = _DIGEST_KEY.match(key)
    if match is None:
        return None
    return _DigestKey(
        uri=uri,
        chain=f"{match['base']}/{match['stem']}",
        end=match["end"],
        is_backfill=match["backfill"] is not None,
    )


def _end_time_of(key: _DigestKey) -> str:
    """키 이름의 끝 시각(YYYYMMDDTHHMMSSZ)을 본문 형식으로 바꾼다."""
    end = datetime.datetime.strptime(key.end, "%Y%m%dT%H%M%SZ")
    return end.strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_s3_uri(uri: str) -> tuple[str, str]:
    bucket, sep, key = uri.removeprefix("s3://").partition("/")
    if not uri.startswith("s3://") or not bucket or not sep or not key:
        raise ValueError(f"s3://버킷/키 형식이 아님: {uri}")
    return bucket, key


def _no_evidence(uri: str, kind: ObjectKind, reason: str) -> Finding:
    return Finding(uri, kind, "evidence_none", reason)
