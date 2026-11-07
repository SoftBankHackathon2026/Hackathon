"""워크로드 env 하나를 분류한다 — EnvEntry 를 만드는 유일한 곳.

수집기는 env 값이 실려 온다고 가정한다(설계 4.2 env 계약). 그래서 값을 남길지
여기서 한 번만 정한다. 비밀로 보이면 suspected_secret, 애매하면 unknown 이고
둘 다 값을 버린다. 값을 남기는 것은 plain_nonsecret 뿐이다.

  entry = env.classify_env("LOG_LEVEL", "info")
"""

import collections
import math
import re
from typing import Literal

from crosscloud import model

# 키에 이 단어가 들어 있으면 비밀로 본다. 키는 대문자, '-' 는 '_' 로 맞춘 뒤
# 부분 문자열로 찾는다(PGPASSWORD 같은 붙여 쓴 이름도 잡는다).
_SECRET_KEY_WORDS = (
    "PASSWORD",
    "PASSWD",
    "PASSPHRASE",
    "SECRET",
    "TOKEN",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
)
# 키를 '_' 로 나눈 조각이 이것과 같으면 비밀로 본다. 짧은 단어라 부분 문자열로
# 찾으면 오탐이 많다.
_SECRET_KEY_SEGMENTS = frozenset({"KEY", "PWD", "PW", "PASS"})
# 조각이 이것으로 끝나도 비밀로 본다(PGPASS·DBPASS·DBPWD 처럼 붙여 쓴 이름).
_SECRET_KEY_SEGMENT_SUFFIXES = ("PASS", "PWD")
# 비밀일 수도 아닐 수도 있는 조각. 값을 버리고 사람이 확인한다.
_AMBIGUOUS_KEY_SEGMENTS = frozenset(
    {"AUTH", "SESSION", "SALT", "DSN", "CERT", "SIGNING"}
)

_SECRET_VALUE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        # 자격 증명이 든 URL. 사용자 이름이 비거나(redis://:pw@) 비밀번호에
        # '/' 가 들어 있어도 잡는다.
        r"://[^/\s:@]*:[^\s@]+@",
        # key=value DSN, JSON·YAML 안의 비밀 키("password": ..., token: ...).
        r"(?i)\b(password|passwd|passphrase|pwd|secret|token|api_?key"
        r"|private_?key|credentials?)[\"']?\s*[:=]",
        r"(?i)^\s*(bearer|basic)\s+\S",  # HTTP Authorization 값
        r"\b(AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS 액세스 키
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\beyJ[\w-]+\.eyJ[\w-]+\.",  # JWT
        r"\bgh[pousr]_[A-Za-z0-9]{20,}",  # GitHub 토큰
        r"\bxox[abprs]-",  # Slack 토큰
        r"\bAIza[0-9A-Za-z_-]{35}\b",  # Google API 키
    )
)

# 토큰처럼 보이는 값: 공백·구분 문자 없이 base64/hex 문자만 길게 이어진다.
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
# 토큰처럼 보이는 값의 문자당 엔트로피(비트)가 이 이상이면 무작위 비밀로 본다.
# 무작위 base64 는 5 를 넘고, 반복이 많은 값은 2 아래다.
_HIGH_ENTROPY_BITS = 3.5


def classify_env(key: str, raw_value: str | model.SecretRef) -> model.EnvEntry:
    """워크로드 env 하나를 분류해 EnvEntry 로 만든다.

    Args:
        key: env 이름.
        raw_value: 평문 env 값, 또는 Secret 서비스 참조(ECS 작업 정의
            secrets · Cloud Run secretKeyRef).

    Returns:
        분류한 EnvEntry. plain_nonsecret 일 때만 값을 담는다.
    """
    if isinstance(raw_value, model.SecretRef):
        return _entry(key, "secret_ref")
    if raw_value == "":
        return _entry(key, "empty")
    if _looks_secret(key, raw_value):
        return _entry(key, "suspected_secret")
    if _is_ambiguous(key, raw_value):
        return _entry(key, "unknown")
    return model.EnvEntry(
        key=key,
        classification="plain_nonsecret",
        value=raw_value,
        confirmed_by=None,
    )


def _entry(
    key: str,
    classification: Literal[
        "secret_ref", "suspected_secret", "unknown", "empty"
    ],
) -> model.EnvEntry:
    return model.EnvEntry(
        key=key, classification=classification, value=None, confirmed_by=None
    )


def _key_segments(key: str) -> tuple[str, frozenset[str]]:
    normalized = key.upper().replace("-", "_")
    return normalized, frozenset(normalized.split("_"))


def _looks_secret(key: str, value: str) -> bool:
    normalized, segments = _key_segments(key)
    if any(word in normalized for word in _SECRET_KEY_WORDS):
        return True
    if segments & _SECRET_KEY_SEGMENTS:
        return True
    if any(
        segment.endswith(_SECRET_KEY_SEGMENT_SUFFIXES) for segment in segments
    ):
        return True
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        return True
    return _is_token_like(value) and _entropy_bits(value) >= _HIGH_ENTROPY_BITS


def _is_ambiguous(key: str, value: str) -> bool:
    _, segments = _key_segments(key)
    if segments & _AMBIGUOUS_KEY_SEGMENTS:
        return True
    if not value.isprintable():
        return True
    return _is_token_like(value)


def _is_token_like(value: str) -> bool:
    if not _TOKEN_LIKE.fullmatch(value):
        return False
    has_letter = any(char.isalpha() for char in value)
    has_digit = any(char.isdigit() for char in value)
    return has_letter and has_digit


def _entropy_bits(value: str) -> float:
    counts = collections.Counter(value)
    total = len(value)
    return -sum(
        count / total * math.log2(count / total) for count in counts.values()
    )
