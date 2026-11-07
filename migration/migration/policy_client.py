"""OPA 정책 판정 클라이언트와 실행 차단 gate (POL-01·POL-02).

원칙
  - OPA HTTP 200 은 허용이 아니다. result.decision 의 형식을 검증하고 outcome == "allow" 일 때만 allowed 다.
  - OPA 장애·timeout·형식 오류는 outcome "error" 로 기록하고 실행하지 않는다.
  - 모든 판정은 DecisionStore(sqlite) 에 남긴다: job/stage/plan digest/evidence snapshot/policy version/이유.
  - 판정은 계획 digest 와 증거 snapshot 에 묶인다. 둘 중 하나라도 바뀌면 옛 판정을 재사용하지 않는다.

실행 제어기(cutover)·관리 API 는 외부 작업 직전에 `gate()` 를 호출하고, 반환된 Decision 의 allowed 가 True 일 때만
한 단계를 실행한다. 정책 Rego 는 Hackathon/policies/migration.rego, 입력 계약은 policies/schema/input.schema.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from .state import _SqliteBase, utcnow_iso

log = logging.getLogger("migration.policy")

SCHEMA_VERSION = "1"
# "remediation" 은 AI 수정안 적용 판정(policies/remediation.rego, package ghost.remediation) 의 단계다.
STAGES: tuple[str, ...] = ("plan", "pre_start", "pre_cutover", "final_verify", "remediation")
DECISION_PATH = "/v1/data/ghost/migration/decision"
POLICY_DB_FILE = "policy.sqlite3"

# Decision.outcome 값. 정책이 내는 4개 + 클라이언트가 내는 error.
ALLOW = "allow"
DENY = "deny"
HOLD = "hold"
INVALID = "invalid"
ERROR = "error"
OUTCOMES: frozenset[str] = frozenset({ALLOW, DENY, HOLD, INVALID, ERROR})
POLICY_OUTCOMES: frozenset[str] = frozenset({ALLOW, DENY, HOLD, INVALID})

# 클라이언트 측 이유 코드 (정책이 아니라 호출 경로의 문제)
POLICY_SERVICE_UNAVAILABLE = "POLICY-SERVICE-UNAVAILABLE"
POLICY_RESPONSE_MALFORMED = "POLICY-RESPONSE-MALFORMED"
POLICY_INPUT_INVALID = "POLICY-INPUT-INVALID"

NEXT_ACTION: dict[str, str] = {
    ALLOW: "허용된 한 단계만 실행하고 결과를 기록한다",
    DENY: "위반 항목의 설정을 수정한 뒤 증거를 재수집해 다시 판정한다",
    HOLD: "부족하거나 오래된 증거를 재수집해 다시 판정한다",
    INVALID: "호출자의 입력 계약 오류다. 정책 입력 생성 코드를 고친다",
    ERROR: "정책 서비스 상태를 확인하고 다시 판정한다. 실행하지 않는다",
}


class PolicyError(RuntimeError):
    """정책 서비스 호출·응답 처리 실패. 실행을 막아야 한다."""


class PolicyBlocked(RuntimeError):
    """판정이 allow 가 아니어서 실행을 막았다."""

    def __init__(self, decision: "Decision") -> None:
        super().__init__(f"정책 판정 {decision.outcome} ({', '.join(decision.reason_codes) or '-'}) → 실행 차단")
        self.decision = decision


# ---------------------------------------------------------------------------
# 입력 생성
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest_of(value: Any) -> str:
    """계획·증거처럼 구조화된 값의 안정적인 digest."""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_check(
    status: str,
    detail: Mapping[str, Any],
    *,
    source: str,
    evidence_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """검사 결과 하나를 계약 형식으로 만든다. 수집기가 사용한다."""
    if status not in ("pass", "fail", "unknown", "error"):
        raise ValueError(f"status 는 pass|fail|unknown|error 중 하나다: {status!r}")
    return {
        "status": status,
        "observed_at": observed_at or utcnow_iso(),
        "source": source,
        "evidence_id": evidence_id or f"ev-{uuid.uuid4().hex[:12]}",
        "detail": dict(detail),
    }


def build_input(
    *,
    job_id: str,
    stage: str,
    plan: Mapping[str, Any],
    checks: Mapping[str, Mapping[str, Any]],
    evidence_plan_digest: str | None = None,
    snapshot_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """정책 입력을 조립한다. plan 에는 digest·limits 가 있어야 한다. 비밀값은 넣지 않는다.

    evidence_plan_digest 는 증거를 수집할 때 본 계획의 digest 다. 생략하면 plan.digest 를 쓴다
    (= 지금 수집했다는 뜻). snapshot_id 를 생략하면 checks 내용의 digest 로 만든다.
    """
    if stage not in STAGES:
        raise ValueError(f"stage 는 {STAGES} 중 하나다: {stage!r}")
    if not isinstance(plan.get("digest"), str) or not plan["digest"]:
        raise ValueError("plan.digest 가 없다. digest_of(plan_without_digest) 로 먼저 만든다")
    checks_dict = {name: dict(c) for name, c in checks.items()}
    at = now or datetime.now(UTC)
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "stage": stage,
        "now": at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "plan": dict(plan),
        "evidence": {
            "snapshot_id": snapshot_id or ("snap-" + digest_of(checks_dict)[7:23]),
            "plan_digest": evidence_plan_digest or plan["digest"],
            "checks": checks_dict,
        },
    }


def validate_input(policy_input: Mapping[str, Any]) -> list[str]:
    """OPA 에 보내기 전 최소 계약 검사. 문제 목록을 돌려준다 (비어 있으면 통과)."""
    problems: list[str] = []
    if policy_input.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version")
    if policy_input.get("stage") not in STAGES:
        problems.append("stage")
    if not isinstance(policy_input.get("job_id"), str) or not policy_input["job_id"]:
        problems.append("job_id")
    if not isinstance(policy_input.get("now"), str):
        problems.append("now")
    plan = policy_input.get("plan")
    if not isinstance(plan, Mapping) or not isinstance(plan.get("digest"), str) or not isinstance(plan.get("limits"), Mapping):
        problems.append("plan")
    evidence = policy_input.get("evidence")
    if (
        not isinstance(evidence, Mapping)
        or not isinstance(evidence.get("snapshot_id"), str)
        or not isinstance(evidence.get("checks"), Mapping)
    ):
        problems.append("evidence")
    if _contains_secret_like_key(policy_input):
        problems.append("secret-like key (password/secret/token/dsn)")
    return problems


_SECRET_KEY_PARTS = ("password", "secret", "token", "dsn", "conninfo")


def _contains_secret_like_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for k, v in value.items():
            if any(p in str(k).lower() for p in _SECRET_KEY_PARTS):
                return True
            if _contains_secret_like_key(v):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_like_key(v) for v in value)
    return False


# ---------------------------------------------------------------------------
# 판정
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """한 번의 판정. allowed 는 outcome == "allow" 일 때만 True 다."""

    decision_id: str
    job_id: str
    stage: str
    outcome: str
    reason_codes: tuple[str, ...]
    violations: tuple[dict[str, Any], ...]
    policy_version: str | None
    plan_digest: str | None
    evidence_snapshot_id: str | None
    input_digest: str
    evaluated_at: str
    evaluator: str  # OPA URL 또는 "opa-cli:<dir>"
    opa_decision_id: str | None = None
    error: str | None = None
    raw_result: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def next_action(self) -> str:
        return NEXT_ACTION[self.outcome]

    def binds(self, *, plan_digest: str, evidence_snapshot_id: str) -> bool:
        """이 판정이 지금의 계획·증거에 대한 것인지. 아니면 재사용 금지."""
        return self.plan_digest == plan_digest and self.evidence_snapshot_id == evidence_snapshot_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "job_id": self.job_id,
            "stage": self.stage,
            "allowed": self.allowed,
            "outcome": self.outcome,
            "reason_codes": list(self.reason_codes),
            "violations": [dict(v) for v in self.violations],
            "policy_version": self.policy_version,
            "plan_digest": self.plan_digest,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "input_digest": self.input_digest,
            "evaluated_at": self.evaluated_at,
            "evaluator": self.evaluator,
            "opa_decision_id": self.opa_decision_id,
            "error": self.error,
            "next_action": self.next_action,
        }


def parse_decision_result(result: Any) -> dict[str, Any]:
    """OPA 가 돌려준 decision 객체의 형식을 엄격히 검사한다. 통과 못 하면 PolicyError."""
    if not isinstance(result, Mapping):
        raise PolicyError(f"decision 이 객체가 아니다: {type(result).__name__}")
    outcome = result.get("outcome")
    if outcome not in POLICY_OUTCOMES:
        raise PolicyError(f"decision.outcome 이 allow|deny|hold|invalid 가 아니다: {outcome!r}")
    allowed = result.get("allowed")
    if not isinstance(allowed, bool) or allowed != (outcome == ALLOW):
        raise PolicyError(f"decision.allowed({allowed!r}) 와 outcome({outcome}) 이 맞지 않는다")
    codes = result.get("reason_codes")
    if not isinstance(codes, list) or not all(isinstance(c, str) and c for c in codes):
        raise PolicyError("decision.reason_codes 는 문자열 배열이어야 한다")
    violations = result.get("violations")
    if not isinstance(violations, list) or not all(isinstance(v, Mapping) and isinstance(v.get("code"), str) for v in violations):
        raise PolicyError("decision.violations 는 {code,...} 객체 배열이어야 한다")
    if outcome == ALLOW and (codes or violations):
        raise PolicyError("allow 인데 이유 코드/위반이 있다")
    if outcome != ALLOW and not codes:
        raise PolicyError(f"{outcome} 인데 이유 코드가 없다")
    if not isinstance(result.get("policy_version"), str) or not result["policy_version"]:
        raise PolicyError("decision.policy_version 이 없다")
    return dict(result)


class Evaluator(Protocol):
    """정책 입력을 평가해 (decision 객체, OPA decision_id) 를 돌려준다. 실패는 PolicyError."""

    name: str

    def evaluate(self, policy_input: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]: ...


class OpaHttpEvaluator:
    """OPA REST Data API. POST {base_url}/v1/data/ghost/migration/decision."""

    def __init__(self, base_url: str, *, timeout: float = 5.0, path: str = DECISION_PATH) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.path = path
        self.name = self.base_url

    def evaluate(self, policy_input: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        body = json.dumps({"input": policy_input}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + self.path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - 설정된 내부 URL
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raise PolicyError(f"OPA HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PolicyError(f"OPA 연결 실패/timeout ({self.base_url}): {exc}") from None
        if status != 200:
            raise PolicyError(f"OPA HTTP {status}")
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PolicyError(f"OPA 응답이 JSON 이 아니다: {exc}") from None
        if not isinstance(doc, Mapping) or "result" not in doc:
            # 정책이 없거나 경로가 틀리면 OPA 는 200 + {} 를 준다. 이것은 허용이 아니다.
            raise PolicyError("OPA 응답에 result 가 없다 (정책 미적재 또는 경로 오류)")
        opa_id = doc.get("decision_id")
        return parse_decision_result(doc["result"]), (str(opa_id) if opa_id else None)


class OpaCliEvaluator:
    """로컬 `opa eval` 로 평가한다 (서버 없이 개발·테스트할 때). 운영 경로가 아니다."""

    def __init__(self, policy_dir: str | Path, *, opa_bin: str = "opa", timeout: float = 30.0,
                 rego: str = "migration.rego", query: str = "data.ghost.migration.decision") -> None:
        self.policy_dir = Path(policy_dir)
        self.opa_bin = opa_bin
        self.timeout = timeout
        self.rego = rego
        self.query = query
        self.name = f"opa-cli:{self.policy_dir / rego}"

    @staticmethod
    def available(opa_bin: str = "opa") -> bool:
        return shutil.which(opa_bin) is not None

    def evaluate(self, policy_input: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
        rego = self.policy_dir / self.rego
        if not rego.is_file():
            raise PolicyError(f"정책 파일이 없다: {rego}")
        cmd = [self.opa_bin, "eval", "--format", "json", "-d", str(rego), "--stdin-input", self.query]
        try:
            proc = subprocess.run(  # noqa: S603 - 고정 인자
                cmd, input=json.dumps(policy_input, ensure_ascii=False), capture_output=True, text=True,
                timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyError(f"opa eval 실행 실패: {exc}") from None
        if proc.returncode != 0:
            raise PolicyError(f"opa eval 종료 코드 {proc.returncode}: {proc.stderr.strip()[:300]}")
        try:
            doc = json.loads(proc.stdout)
            value = doc["result"][0]["expressions"][0]["value"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PolicyError(f"opa eval 출력 해석 실패: {exc}") from None
        return parse_decision_result(value), None


# ---------------------------------------------------------------------------
# 판정 기록
# ---------------------------------------------------------------------------

_SCHEMA_POLICY = """
CREATE TABLE IF NOT EXISTS policy_decisions (
    decision_id          TEXT PRIMARY KEY,
    job_id               TEXT NOT NULL,
    stage                TEXT NOT NULL,
    outcome              TEXT NOT NULL,
    allowed              INTEGER NOT NULL,
    reason_codes         TEXT NOT NULL,
    violations           TEXT NOT NULL,
    policy_version       TEXT,
    plan_digest          TEXT,
    evidence_snapshot_id TEXT,
    input_digest         TEXT NOT NULL,
    input_json           TEXT NOT NULL,
    raw_result           TEXT,
    evaluator            TEXT NOT NULL,
    opa_decision_id      TEXT,
    error                TEXT,
    evaluated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS policy_decisions_job_idx ON policy_decisions(job_id, stage, evaluated_at);
"""


class DecisionStore(_SqliteBase):
    """판정 기록. 입력 전체를 남기므로 입력에 비밀값이 없어야 한다 (validate_input 이 막는다)."""

    def __init__(self, state_dir: str | Path) -> None:
        super().__init__(Path(state_dir) / POLICY_DB_FILE, _SCHEMA_POLICY)

    def record(self, decision: Decision, policy_input: Mapping[str, Any]) -> None:
        self._exec(
            "INSERT INTO policy_decisions (decision_id, job_id, stage, outcome, allowed, reason_codes, violations, "
            "policy_version, plan_digest, evidence_snapshot_id, input_digest, input_json, raw_result, evaluator, "
            "opa_decision_id, error, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision.decision_id, decision.job_id, decision.stage, decision.outcome, int(decision.allowed),
                json.dumps(list(decision.reason_codes)), json.dumps(list(decision.violations), ensure_ascii=False),
                decision.policy_version, decision.plan_digest, decision.evidence_snapshot_id, decision.input_digest,
                canonical_json(policy_input),
                canonical_json(decision.raw_result) if decision.raw_result is not None else None,
                decision.evaluator, decision.opa_decision_id, decision.error, decision.evaluated_at,
            ),
        )

    def latest(self, job_id: str, stage: str) -> Decision | None:
        cur = self._exec(
            "SELECT * FROM policy_decisions WHERE job_id = ? AND stage = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT 1",
            (job_id, stage),
        )
        row = cur.fetchone()
        return self._to_decision(cur, row) if row else None

    def history(self, job_id: str, limit: int = 50) -> list[Decision]:
        cur = self._exec(
            "SELECT * FROM policy_decisions WHERE job_id = ? ORDER BY evaluated_at DESC, rowid DESC LIMIT ?",
            (job_id, limit),
        )
        return [self._to_decision(cur, row) for row in cur.fetchall()]

    def get(self, decision_id: str) -> Decision | None:
        cur = self._exec("SELECT * FROM policy_decisions WHERE decision_id = ?", (decision_id,))
        row = cur.fetchone()
        return self._to_decision(cur, row) if row else None

    def input_for(self, decision_id: str) -> dict[str, Any] | None:
        """재현용 원본 입력."""
        cur = self._exec("SELECT input_json FROM policy_decisions WHERE decision_id = ?", (decision_id,))
        row = cur.fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _to_decision(cur: Any, row: tuple[Any, ...]) -> Decision:
        cols = [d[0] for d in cur.description]
        r = dict(zip(cols, row, strict=True))
        return Decision(
            decision_id=r["decision_id"], job_id=r["job_id"], stage=r["stage"], outcome=r["outcome"],
            reason_codes=tuple(json.loads(r["reason_codes"])),
            violations=tuple(json.loads(r["violations"])),
            policy_version=r["policy_version"], plan_digest=r["plan_digest"],
            evidence_snapshot_id=r["evidence_snapshot_id"], input_digest=r["input_digest"],
            evaluated_at=r["evaluated_at"], evaluator=r["evaluator"], opa_decision_id=r["opa_decision_id"],
            error=r["error"], raw_result=json.loads(r["raw_result"]) if r["raw_result"] else None,
        )


# ---------------------------------------------------------------------------
# gate — 실행 직전 호출
# ---------------------------------------------------------------------------


def evaluate(evaluator: Evaluator, policy_input: Mapping[str, Any]) -> Decision:
    """평가해 Decision 을 만든다. 어떤 실패도 예외 대신 outcome=error 로 돌려준다 (호출자는 allowed 만 본다)."""
    decision_id = f"dec-{uuid.uuid4().hex[:16]}"
    evaluated_at = utcnow_iso()
    input_digest = digest_of(policy_input)
    job_id = str(policy_input.get("job_id", ""))
    stage = str(policy_input.get("stage", ""))
    plan_digest = (policy_input.get("plan") or {}).get("digest") if isinstance(policy_input.get("plan"), Mapping) else None
    snapshot_id = (policy_input.get("evidence") or {}).get("snapshot_id") if isinstance(policy_input.get("evidence"), Mapping) else None

    problems = validate_input(policy_input)
    if problems:
        log.error("정책 입력 계약 위반 job=%s stage=%s: %s", job_id, stage, problems)
        return Decision(
            decision_id, job_id, stage, ERROR, (POLICY_INPUT_INVALID,), (), None, plan_digest, snapshot_id,
            input_digest, evaluated_at, evaluator.name, error="입력 계약 위반: " + ", ".join(problems),
        )
    try:
        result, opa_id = evaluator.evaluate(policy_input)
    except PolicyError as exc:
        code = POLICY_RESPONSE_MALFORMED if "decision" in str(exc) or "result" in str(exc) else POLICY_SERVICE_UNAVAILABLE
        log.error("정책 평가 실패 job=%s stage=%s: %s", job_id, stage, exc)
        return Decision(
            decision_id, job_id, stage, ERROR, (code,), (), None, plan_digest, snapshot_id,
            input_digest, evaluated_at, evaluator.name, error=str(exc),
        )
    # 정책이 본 job/stage/plan/snapshot 이 우리가 보낸 것과 같아야 한다.
    if result.get("job_id") != job_id or result.get("stage") != stage:
        return Decision(
            decision_id, job_id, stage, ERROR, (POLICY_RESPONSE_MALFORMED,), (), result.get("policy_version"),
            plan_digest, snapshot_id, input_digest, evaluated_at, evaluator.name, opa_id,
            error=f"응답의 job/stage({result.get('job_id')}/{result.get('stage')}) 가 입력과 다르다", raw_result=result,
        )
    decision = Decision(
        decision_id=decision_id, job_id=job_id, stage=stage, outcome=str(result["outcome"]),
        reason_codes=tuple(result["reason_codes"]), violations=tuple(dict(v) for v in result["violations"]),
        policy_version=str(result["policy_version"]), plan_digest=result.get("plan_digest") or plan_digest,
        evidence_snapshot_id=result.get("evidence_snapshot_id") or snapshot_id, input_digest=input_digest,
        evaluated_at=evaluated_at, evaluator=evaluator.name, opa_decision_id=opa_id, raw_result=result,
    )
    log.info("정책 판정 job=%s stage=%s outcome=%s codes=%s policy=%s", job_id, stage, decision.outcome,
             ",".join(decision.reason_codes) or "-", decision.policy_version)
    return decision


def gate(evaluator: Evaluator, store: DecisionStore, policy_input: Mapping[str, Any]) -> Decision:
    """평가하고 기록한다. 실행 제어기는 반환값의 allowed 가 True 일 때만 다음 외부 작업을 한다."""
    decision = evaluate(evaluator, policy_input)
    store.record(decision, policy_input)
    return decision


def require_allowed(decision: Decision) -> Decision:
    """allowed 가 아니면 PolicyBlocked. 실행 함수 첫 줄에서 쓴다."""
    if not decision.allowed:
        raise PolicyBlocked(decision)
    return decision


def reusable(decision: Decision | None, *, plan_digest: str, evidence_snapshot_id: str, max_age_seconds: float) -> bool:
    """이미 있는 allow 판정을 재사용해도 되는지. 계획·증거가 같고 충분히 최근일 때만. 기본은 재판정이다."""
    if decision is None or not decision.allowed:
        return False
    if not decision.binds(plan_digest=plan_digest, evidence_snapshot_id=evidence_snapshot_id):
        return False
    age = (datetime.now(UTC) - datetime.fromisoformat(decision.evaluated_at)).total_seconds()
    return 0 <= age <= max_age_seconds
