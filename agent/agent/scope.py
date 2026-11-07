"""수정 제안과 정적 검사. 무엇을 고칠 수 있는지는 AI 가 아니라 이 규칙이 정한다.

  S0 빈 제안 / S1 허용 키 밖 / S2 계획 값과 다름 / S3 비밀값 키 / S4 형식 오류
  S5 현재 값과 같음(고칠 것 없음) / S6 같은 키 중복 / S7 제안과 실제 파일 수정 불일치

문제를 전부 모아 돌려준다. 비어 있을 때만 다음 단계로 간다. 거절 메시지에 제안 값을 넣지 않는다
(허용 키 밖의 값은 비밀값일 수 있다). 키 이름도 형식이 맞을 때만 넣는다.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping

ALLOWED_KEYS: frozenset[str] = frozenset({"DB_HOST", "DB_PORT"})
# AI 에게 보여 주는 키. 이 밖의 키(비밀번호 등)는 파일에 쓰지 않는다.
VISIBLE_KEYS: tuple[str, ...] = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER")
SECRET_KEY_PARTS: tuple[str, ...] = ("password", "passwd", "pwd", "secret", "token", "dsn", "conninfo", "credential", "private")

_HOST_RE = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


@dataclass(frozen=True)
class Change:
    key: str
    value: str


@dataclass(frozen=True)
class Proposal:
    changes: tuple[Change, ...]
    cause: str
    impact: str
    verification: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Proposal":
        return cls(
            changes=tuple(Change(c["key"], c["value"]) for c in raw["changes"]),
            cause=raw["cause"], impact=raw["impact"], verification=raw["verification"],
        )

    def values(self) -> dict[str, str]:
        return {c.key: c.value for c in self.changes}


@dataclass(frozen=True)
class Rejection:
    code: str
    message: str


def _safe_key(key: str) -> str:
    return key if _KEY_RE.fullmatch(key) else "<형식 오류 키>"


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)


def _valid_host(value: str) -> bool:
    return _HOST_RE.fullmatch(value) is not None


def _valid_port(value: str) -> bool:
    return _PORT_RE.fullmatch(value) is not None and 1 <= int(value) <= 65535


def check_proposal(
    proposal: Proposal,
    *,
    current: Mapping[str, str],
    target_db: Mapping[str, Any],
    file_changes: Mapping[str, str] | None = None,
) -> list[Rejection]:
    """정적 검사. current 는 저장소의 현재 값, target_db 는 계획서 값, file_changes 는 AI 가 실제로 고친 파일 내용이다."""
    if not proposal.changes:
        return [Rejection("S0-EMPTY", "제안이 비어 있다")]

    planned = {"DB_HOST": str(target_db["host"]), "DB_PORT": str(target_db["port"])}
    problems: list[Rejection] = []
    seen: set[str] = set()
    effective = False
    for change in proposal.changes:
        key, value = change.key, change.value
        name = _safe_key(key)
        if key in seen:
            problems.append(Rejection("S6-DUPLICATE-KEY", f"키 {name} 가 두 번 나온다"))
        seen.add(key)
        if _is_secret_key(key):
            problems.append(Rejection("S3-SECRET-KEY", f"비밀값 키 {name} 는 건드릴 수 없다"))
            continue
        if key not in ALLOWED_KEYS:
            problems.append(Rejection("S1-KEY-NOT-ALLOWED", f"키 {name} 는 허용 목록 밖이다"))
            continue
        valid = _valid_host(value) if key == "DB_HOST" else _valid_port(value)
        if not valid:
            problems.append(Rejection("S4-FORMAT", f"{key} 값의 형식이 틀렸다"))
            continue
        if value != planned[key]:
            problems.append(Rejection("S2-VALUE-NOT-IN-PLAN", f"{key} 값이 계획서 plan.target_db 와 다르다"))
            continue
        if str(current.get(key, "")) != value:
            effective = True

    if not problems and not effective:
        problems.append(Rejection("S5-NO-CHANGE", "제안 값이 현재 값과 같다. 설정 문제가 아닐 수 있다"))
    if file_changes is not None and dict(file_changes) != proposal.values():
        problems.append(Rejection("S7-FILE-MISMATCH", "보고한 변경과 실제 파일 변경이 다르다"))
    return problems
