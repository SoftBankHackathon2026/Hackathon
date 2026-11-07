"""AI 가 바꿀 수 있는 범위와 정적 검사(S1~S4). 정책(remediation.rego)과 같은 규칙을 호출 쪽에서 먼저 거른다.

S1 범위   허용 키만 변경
S2 값     계획에 등록된 값만 (호스트는 plan.target_db.host, 포트는 plan.target_db.port)
S3 형식   키 이름·값이 문자열이고 제어문자 없음
S4 비밀값 비밀값 키 변경 없음, 값 안에 비밀값 패턴 없음
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from migration.policy_client import digest_of

from .config import ScopeConfig
from .stores import _is_secret_key


@dataclass(frozen=True)
class Change:
    key: str
    old: str
    new: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "old": self.old, "new": self.new}


@dataclass(frozen=True)
class Proposal:
    """제안 하나. digest 는 changes 만으로 만들어 검증·승인·적용이 같은 내용을 가리키게 한다."""

    changes: tuple[Change, ...]
    analysis: str
    root_cause: str
    impact: str
    verification_plan: str
    cannot_fix_reason: str | None = None
    proposer: str = "unknown"
    model: str | None = None

    @property
    def digest(self) -> str:
        return digest_of([c.as_dict() for c in self.changes])

    def apply_to(self, values: Mapping[str, str]) -> dict[str, str]:
        out = dict(values)
        for c in self.changes:
            out[c.key] = c.new
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "changes": [c.as_dict() for c in self.changes],
            "analysis": self.analysis,
            "root_cause": self.root_cause,
            "impact": self.impact,
            "verification_plan": self.verification_plan,
            "cannot_fix_reason": self.cannot_fix_reason,
            "proposer": self.proposer,
            "model": self.model,
        }


def diff_values(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[Change, ...]:
    """Codex 가 고친 파일과 원본의 차이. 키 추가·삭제도 변경으로 본다(삭제는 new="")."""
    changes: list[Change] = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key, ""), after.get(key, "")
        if old != new:
            changes.append(Change(key, old, new))
    return tuple(changes)


def allowed_values(scope: ScopeConfig, plan: Mapping[str, Any]) -> dict[str, list[str]]:
    """계획이 허용하는 값 목록. 호스트·포트 외의 허용 키는 값 제한 없음(키 제한만)."""
    target = plan.get("target_db") or {}
    out: dict[str, list[str]] = {}
    if target.get("host"):
        out[scope.host_key] = [str(target["host"])]
    out[scope.port_key] = [str(target.get("port", 5432))]
    return out


def static_checks(scope: ScopeConfig, plan: Mapping[str, Any], proposal: Proposal) -> list[dict[str, Any]]:
    """S1~S4. 각 항목 {name, status: pass|fail, detail}. 하나라도 fail 이면 정책에 보내지 않는다."""
    allowed = allowed_values(scope, plan)
    results: list[dict[str, Any]] = []

    bad_keys = [c.key for c in proposal.changes if c.key not in scope.allowed_keys]
    results.append({"name": "S1-scope", "status": "fail" if bad_keys else "pass",
                    "detail": {"allowed_keys": list(scope.allowed_keys), "violating_keys": bad_keys}})

    bad_values = [c.as_dict() for c in proposal.changes if c.key in allowed and c.new not in allowed[c.key]]
    results.append({"name": "S2-value", "status": "fail" if bad_values else "pass",
                    "detail": {"allowed_values": allowed, "violating": bad_values}})

    bad_format = [c.key for c in proposal.changes
                  if not c.key.isidentifier() or any(ord(ch) < 32 for ch in c.new) or len(c.new) > 512]
    results.append({"name": "S3-format", "status": "fail" if bad_format else "pass", "detail": {"violating_keys": bad_format}})

    secret_keys = [c.key for c in proposal.changes if _is_secret_key(c.key, scope.secret_keys)]
    results.append({"name": "S4-secret", "status": "fail" if secret_keys else "pass", "detail": {"violating_keys": secret_keys}})

    empty = len(proposal.changes) == 0
    results.append({"name": "S0-nonempty", "status": "fail" if empty else "pass",
                    "detail": {"changes": len(proposal.changes), "cannot_fix_reason": proposal.cannot_fix_reason}})
    return results


def all_passed(results: list[dict[str, Any]]) -> bool:
    return all(r["status"] == "pass" for r in results)
