"""옮긴 CloudTrail 로그·다이제스트의 오프라인 무결성 검증기.

aws-cli `validate-logs`(awscli/customizations/cloudtrail/validation.py)를
참조한다. 다른 점은 셋이다(RC-A21 오프라인 3요건).

*   서명은 S3 메타데이터에서 읽지 않고, 이관 전에 받아 둔 값(bundle)을 쓴다.
*   공개키는 운영자가 보관한 ListPublicKeys 결과를 입력으로 받는다(지문 →
    PKCS#1 DER). 검증 대상과 같은 곳에서 온 키는 신뢰 기준이 아니다.
*   "원래 위치에 있는가" 검사는 경로 대응표 검사로 바꾼다. 객체는 원래
    S3 경로(s3://버킷/키)로 부르고, 새 위치에서 읽는 일은 호출자의 read
    함수가 맡는다. 다이제스트 본문의 경로가 그 원래 경로와 같아야 한다.

체인 양 끝(시작·끝 다이제스트)은 이관 시점에 따로 기록해 두고 받는다
(chain_bounds). 그래야 경계 다이제스트를 목록째 지운 위조를 missing 으로
잡는다.

backfill 체인은 검증하지 않고, 검증이 꺼진 구간·체인 공백과 함께 "증거
없음"(evidence: none)으로 표시한다. 무결성 확인으로 올리지 않는다 — 서명으로
확인한 객체가 하나도 없으면 결과는 no_evidence 다.

  report = cloudtrail_verify.verify_cloudtrail(
      object_uris=uris, signatures=sigs, public_keys=keys, read=read,
      chain_bounds=recorded_bounds)
  if report.result != "valid": ...
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
Result = Literal["valid", "invalid", "no_evidence"]
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
class ChainBounds:
    """이관 시점에 기록한 체인 하나의 양 끝 다이제스트(원래 경로).

    순회가 끝난 뒤 남은 이전 링크는 oldest 에서만 허용한다(복사 범위의
    시작). newest·oldest 가 아카이브에 없으면 경계가 삭제된 것이다.
    """

    oldest: str
    newest: str


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
    """검증 결과.

    evidence_none 은 실패가 아니지만 무결성 확인도 아니다. 그래서 서명으로
    확인된 객체가 하나도 없으면 결과는 valid 가 아니라 no_evidence 다.
    """

    findings: tuple[Finding, ...]
    evidence_none_spans: tuple[EvidenceNoneSpan, ...]

    @property
    def result(self) -> Result:
        """invalid·missing 이 있으면 invalid, valid 가 없으면 no_evidence."""
        if any(f.status in ("invalid", "missing") for f in self.findings):
            return "invalid"
        if not any(f.status == "valid" for f in self.findings):
            return "no_evidence"
        return "valid"


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
    chain_bounds: Mapping[str, ChainBounds],
) -> VerificationReport:
    """옮긴 CloudTrail 객체들을 다이제스트 체인으로 검증한다.

    Args:
        object_uris: 옮긴 모든 객체의 원래 경로(s3://버킷/키). 로그·다이제스트.
        signatures: 다이제스트 원래 경로 → 이관 전에 받아 둔 서명.
        public_keys: 공개키 지문 → PKCS#1 DER 바이트(ListPublicKeys Value).
        read: 원래 경로 → 새 위치의 원본 바이트. 없으면 None.
        chain_bounds: 체인 이름 → 이관 시점에 기록한 양 끝(chain_bounds()
            결과). 검증 대상 목록과 따로 보관한 기록이어야 경계 삭제를
            잡는다. 기록에 있는 체인이 통째로 없어도 missing 이다.

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
    for chain in sorted(chain_bounds.keys() - chains.keys()):
        bounds = chain_bounds[chain]
        for uri in dict.fromkeys((bounds.newest, bounds.oldest)):
            findings.append(Finding(uri, "digest", "missing", "기록된 체인"))
    for chain, keys in sorted(chains.items()):
        result = verifier.verify_chain(chain, keys, chain_bounds.get(chain))
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


def chain_bounds(digest_uris: Iterable[str]) -> dict[str, ChainBounds]:
    """다이제스트 원래 경로들에서 체인별 양 끝을 뽑는다(backfill 제외).

    이관 시점에 이 결과를 검증 대상과 따로 기록해 두고, 검증 때
    verify_cloudtrail 의 chain_bounds 로 넘긴다.
    """
    chains = collections.defaultdict(list)
    for uri in digest_uris:
        key = _parse_digest_key(uri)
        if key is not None and not key.is_backfill:
            chains[key.chain].append(key)
    return {
        chain: ChainBounds(
            oldest=min(keys, key=lambda k: k.end).uri,
            newest=max(keys, key=lambda k: k.end).uri,
        )
        for chain, keys in chains.items()
    }


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
        "result": report.result,
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
        self, chain: str, keys: list[_DigestKey], bounds: ChainBounds | None
    ) -> VerificationReport:
        ordered = sorted(keys, key=lambda k: k.end, reverse=True)
        findings = _missing_bounds(ordered, bounds)
        spans: list[EvidenceNoneSpan] = []
        newer: dict[str, Any] | None = None
        for key in ordered:
            linked: dict[str, Any] | None = None
            if newer is not None:
                linked, link_findings = self._follow_link(
                    newer, key, chain, spans
                )
                findings.extend(link_findings)
            newer = None
            raw = self._read(key.uri)
            if raw is None:
                findings.append(Finding(key.uri, "digest", "missing", "없음"))
                continue
            try:
                document = self._verify_digest(key.uri, raw)
            except _DigestError as e:
                findings.append(Finding(key.uri, "digest", "invalid", str(e)))
                continue
            findings.append(self._digest_finding(key.uri, linked))
            findings.extend(self._verify_logs(document))
            newer = document
        if newer is not None:
            findings.extend(_leftover_link(newer, ordered[-1], bounds))
        return VerificationReport(_unique(findings), tuple(spans))

    def _follow_link(
        self,
        newer: dict[str, Any],
        older: _DigestKey,
        chain: str,
        spans: list[EvidenceNoneSpan],
    ) -> tuple[dict[str, Any] | None, list[Finding]]:
        """새 다이제스트의 이전 링크가 바로 다음 다이제스트를 가리키는지 본다.

        링크가 null 이면 체인이 거기서 새로 시작했다(검증 꺼짐·로깅 중단) —
        그 사이는 증거 없음 구간이다. 링크가 다른 키를 가리키면 그 다이제스트가
        사라진 것이다(아카이브와 bundle 양쪽에서 지워진 경우).

        Returns:
            (키가 맞으면 새 다이제스트 문서 — 버킷·서명 대조용, 판정들).
        """
        previous_key = newer["previousDigestS3Object"]
        if not previous_key:
            spans.append(
                EvidenceNoneSpan(
                    chain=chain,
                    start=_end_time_of(older),
                    end=newer["digestStartTime"],
                    reason="체인이 새로 시작함(검증 꺼짐 또는 로깅 중단)",
                )
            )
            return None, []
        _, older_key = _split_s3_uri(older.uri)
        if previous_key == older_key:
            return newer, []
        return None, [_previous_missing(newer)]

    def _digest_finding(
        self, uri: str, newer: dict[str, Any] | None
    ) -> Finding:
        """서명이 맞은 다이제스트의 판정. 새 다이제스트의 링크도 대조한다."""
        if newer is None:
            return Finding(uri, "digest", "valid", "서명 일치")
        bucket, _ = _split_s3_uri(uri)
        signature = self._signatures[uri].signature
        if (
            newer["previousDigestS3Bucket"] != bucket
            or newer["previousDigestSignature"] != signature
        ):
            return Finding(
                uri,
                "digest",
                "invalid",
                "새 다이제스트의 이전 link(버킷·서명)와 맞지 않음",
            )
        return Finding(uri, "digest", "valid", "서명·체인 link 일치")

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
        _log_entries(document)
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
        for uri, expected_hash in _log_entries(document):
            raw = self._read(uri)
            if raw is None:
                findings.append(Finding(uri, "log", "missing", "없음"))
                continue
            findings.append(_verify_log(uri, raw, expected_hash))
        return findings


def _missing_bounds(
    ordered: list[_DigestKey], bounds: ChainBounds | None
) -> list[Finding]:
    """기록된 체인 양 끝이 아카이브 목록에 없으면 missing."""
    if bounds is None:
        return []
    present = {key.uri for key in ordered}
    return [
        Finding(uri, "digest", "missing", "기록된 체인 끝")
        for uri in dict.fromkeys((bounds.newest, bounds.oldest))
        if uri not in present
    ]


def _leftover_link(
    oldest: dict[str, Any], oldest_key: _DigestKey, bounds: ChainBounds | None
) -> list[Finding]:
    """순회가 끝났는데 이전 링크가 남으면, 기록된 체인 시작일 때만 정상."""
    if not oldest["previousDigestS3Object"]:
        return []
    if bounds is not None and oldest_key.uri == bounds.oldest:
        return []
    return [_previous_missing(oldest)]


def _previous_missing(document: dict[str, Any]) -> Finding:
    previous_uri = (
        f"s3://{document['previousDigestS3Bucket']}/"
        f"{document['previousDigestS3Object']}"
    )
    return Finding(previous_uri, "digest", "missing", "체인 링크가 가리킴")


def _unique(findings: list[Finding]) -> tuple[Finding, ...]:
    """같은 객체의 같은 판정이 경계 검사와 링크 검사에서 두 번 나오면 하나로."""
    seen: set[tuple[str, str]] = set()
    unique = []
    for finding in findings:
        if (finding.uri, finding.status) not in seen:
            seen.add((finding.uri, finding.status))
            unique.append(finding)
    return tuple(unique)


def _log_entries(document: dict[str, Any]) -> list[tuple[str, str]]:
    """다이제스트의 logFiles 를 (원래 경로, 기대 해시) 목록으로 읽는다.

    Raises:
        _DigestError: logFiles 형식이 틀렸을 때. 없거나 null 이면 빈 목록.
    """
    log_files = document.get("logFiles") or []
    fields = ("s3Bucket", "s3Object", "hashValue")
    if not isinstance(log_files, list) or not all(
        isinstance(log, dict)
        and all(isinstance(log.get(f), str) for f in fields)
        for log in log_files
    ):
        raise _DigestError("logFiles 형식 오류")
    return [
        (f"s3://{log['s3Bucket']}/{log['s3Object']}", log["hashValue"])
        for log in log_files
    ]


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
