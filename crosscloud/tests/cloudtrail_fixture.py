"""시험용 CloudTrail 다이제스트 체인 — 시험용 RSA 키로 서명한 가짜 trail.

실제 CloudTrail 과 같은 규칙으로 만든다(RC-A20·RC-A21, aws-cli
validation.py 참조): 다이제스트는 gzip JSON, 서명 문자열은
`digestEndTime\\n버킷/키\\nhex(sha256(압축 해제 다이제스트))\\n이전 서명`,
서명은 SHA256withRSA(PKCS#1 v1.5), 공개키는 PKCS#1 DER.
"""

import dataclasses
import datetime
import functools
import gzip
import hashlib
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ACCOUNT = "111122223333"
REGION = "ap-northeast-2"
BUCKET = "trail-bucket"
FINGERPRINT = "0123456789abcdef0123456789abcdef"
START = datetime.datetime(2026, 11, 7, 0, 0, tzinfo=datetime.UTC)
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@functools.cache
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_key_der() -> bytes:
    return (
        private_key()
        .public_key()
        .public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.PKCS1
        )
    )


@dataclasses.dataclass
class FakeTrail:
    """원래 위치(s3://…) 기준으로 본 trail 버킷 내용."""

    objects: dict[str, bytes] = dataclasses.field(default_factory=dict)
    signatures: dict[str, str] = dataclasses.field(default_factory=dict)
    digest_uris: list[str] = dataclasses.field(default_factory=list)
    log_uris: list[str] = dataclasses.field(default_factory=list)
    _previous: tuple[str, str] | None = None

    def add_hour(self, hour: int, *, validation_on: bool = True) -> None:
        """한 시간분 로그 1개와(검증이 켜져 있으면) 다이제스트 1개를 쌓는다.

        검증이 꺼진 시간은 로그만 쌓고 다이제스트는 없다. 다시 켜진 뒤 첫
        다이제스트는 이전 필드가 null 인 새 체인 시작이 된다(RC-A20).
        """
        start = START + datetime.timedelta(hours=hour)
        end = start + datetime.timedelta(hours=1)
        log_uri, log_body = self._add_log(start)
        if not validation_on:
            self._previous = None
            return
        self._add_digest(start, end, [(log_uri, log_body)])

    def add_backfill_digest(self, hour: int) -> str:
        start = START + datetime.timedelta(hours=hour)
        end = start + datetime.timedelta(hours=1)
        key = _digest_key(end, backfill=True)
        content = json.dumps({"digestS3Object": key}).encode()
        uri = f"s3://{BUCKET}/{key}"
        self.objects[uri] = gzip.compress(content)
        self.digest_uris.append(uri)
        return uri

    def _add_log(self, start: datetime.datetime) -> tuple[str, bytes]:
        stamp = start.strftime("%Y%m%dT%H%MZ")
        key = (
            f"AWSLogs/{ACCOUNT}/CloudTrail/{REGION}/{start:%Y/%m/%d}/"
            f"{ACCOUNT}_CloudTrail_{REGION}_{stamp}_abc.json.gz"
        )
        body = json.dumps({"Records": [{"eventTime": stamp}]}).encode()
        uri = f"s3://{BUCKET}/{key}"
        self.objects[uri] = gzip.compress(body)
        self.log_uris.append(uri)
        return uri, body

    def _add_digest(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
        logs: list[tuple[str, bytes]],
    ) -> None:
        key = _digest_key(end, backfill=False)
        previous_key, previous_signature = self._previous or (None, None)
        document = {
            "awsAccountId": ACCOUNT,
            "digestStartTime": start.strftime(_TIME_FORMAT),
            "digestEndTime": end.strftime(_TIME_FORMAT),
            "digestS3Bucket": BUCKET,
            "digestS3Object": key,
            "digestPublicKeyFingerprint": FINGERPRINT,
            "digestSignatureAlgorithm": "SHA256withRSA",
            "previousDigestS3Bucket": BUCKET if previous_key else None,
            "previousDigestS3Object": previous_key,
            "previousDigestSignature": previous_signature,
            "logFiles": [
                {
                    "s3Bucket": BUCKET,
                    "s3Object": uri.removeprefix(f"s3://{BUCKET}/"),
                    "hashValue": hashlib.sha256(body).hexdigest(),
                    "hashAlgorithm": "SHA-256",
                }
                for uri, body in logs
            ],
        }
        content = json.dumps(document).encode()
        signature = sign(document, content)
        uri = f"s3://{BUCKET}/{key}"
        self.objects[uri] = gzip.compress(content)
        self.signatures[uri] = signature
        self.digest_uris.append(uri)
        self._previous = (key, signature)


def sign(document: dict, content: bytes) -> str:
    previous = document["previousDigestSignature"] or "null"
    to_sign = (
        f"{document['digestEndTime']}\n"
        f"{document['digestS3Bucket']}/{document['digestS3Object']}\n"
        f"{hashlib.sha256(content).hexdigest()}\n"
        f"{previous}"
    ).encode()
    signature = private_key().sign(to_sign, padding.PKCS1v15(), hashes.SHA256())
    return signature.hex()


def _digest_key(end: datetime.datetime, *, backfill: bool) -> str:
    suffix = "_backfill" if backfill else ""
    return (
        f"AWSLogs/{ACCOUNT}/CloudTrail-Digest/{REGION}/{end:%Y/%m/%d}/"
        f"{ACCOUNT}_CloudTrail-Digest_{REGION}_audit_{REGION}_"
        f"{end:%Y%m%dT%H%M%SZ}{suffix}.json.gz"
    )


def chain(hours: int) -> FakeTrail:
    trail = FakeTrail()
    for hour in range(hours):
        trail.add_hour(hour)
    return trail
