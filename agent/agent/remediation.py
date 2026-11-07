"""전체 흐름. Slack 의 `/migrate start` 또는 CLI 가 run() 을 부른다.

  DETECTING  저장소 값으로 대상 DB 탐침 → migration 정책(pre_cutover) 판정
  NO_ACTION  allow → 고칠 것 없음
  HELD       deny/hold 인데 AI 가 다룰 수 있는 사유(APP-01)가 아니면 사람에게 넘김
  PROPOSING  Codex 로 수정안 생성 (재시도 상한)
  VERIFYING  정적 검사 S0~S4 → 수정 값으로 재탐침(remediation_probe) → remediation 정책 판정
  WAITING_APPROVAL  정책 hold(APPROVAL-REQUIRED 등) → Slack 에 승인 요청. /migrate approve 로 재개
  APPLYING/APPLIED  저장소에 허용 키만 써 넣음 (버전 충돌 시 중단)
  RECHECK_*  저장소에서 다시 읽어 재탐침 → pre_cutover 재판정 → 결과 보고

모든 단계는 RemediationStore(sqlite) 에 남고, Slack 에 시작·제안·적용·결과를 보고한다.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from migration import policy_client as pc
from migration.state import _SqliteBase, utcnow_iso

from .config import AgentConfig
from .notify import Notifier, section
from .probe import Prober
from .proposer import ProposalContext, Proposer, ProposerError, values_for_ai
from .scope import Proposal, all_passed, allowed_values, static_checks
from .stores import ConfigStore, Snapshot, StoreError, VersionConflict

log = logging.getLogger("agent.remediation")

REMEDIATION_STAGE = "remediation"
REMEDIATION_DECISION_PATH = "/v1/data/ghost/remediation/decision"
# AI 가 다룰 수 있는 거절 사유. 이 밖의 사유(DB 공개 노출, 위치 위반 등)는 설정 한 줄로 고칠 일이 아니다.
FIXABLE_CODES: frozenset[str] = frozenset({"APP-01-TARGET-DB-UNREACHABLE", "APP-01-ORDER-CHECK-FAILED", "APP-01-UNVERIFIED"})

DETECTING, NO_ACTION, HELD, PROPOSING, PROPOSAL_FAILED = "DETECTING", "NO_ACTION", "HELD", "PROPOSING", "PROPOSAL_FAILED"
VERIFYING, WAITING_APPROVAL, APPLYING, APPLIED = "VERIFYING", "WAITING_APPROVAL", "APPLYING", "APPLIED"
RECHECK_PASSED, RECHECK_FAILED, FAILED = "RECHECK_PASSED", "RECHECK_FAILED", "FAILED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS remediations (
    remediation_id TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL,
    status         TEXT NOT NULL,
    record         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS remediations_job_idx ON remediations(job_id, started_at);
"""


class RemediationStore(_SqliteBase):
    def __init__(self, state_dir: str | Path) -> None:
        super().__init__(Path(state_dir) / "remediation.sqlite3", _SCHEMA)

    def save(self, rec: "Record") -> None:
        self._exec(
            "INSERT INTO remediations (remediation_id, job_id, status, record, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(remediation_id) DO UPDATE SET status = excluded.status, record = excluded.record, updated_at = excluded.updated_at",
            (rec.remediation_id, rec.job_id, rec.status, pc.canonical_json(rec.as_dict()), rec.started_at, utcnow_iso()),
        )

    def get(self, remediation_id: str) -> dict[str, Any] | None:
        row = self._exec("SELECT record FROM remediations WHERE remediation_id = ?", (remediation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def latest(self, job_id: str) -> dict[str, Any] | None:
        row = self._exec("SELECT record FROM remediations WHERE job_id = ? ORDER BY started_at DESC LIMIT 1", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None


@dataclass
class Record:
    remediation_id: str
    job_id: str
    target_env: str
    status: str = DETECTING
    started_at: str = field(default_factory=utcnow_iso)
    finished_at: str | None = None
    store_location: str | None = None
    store_version: str | None = None
    config_masked: dict[str, str] = field(default_factory=dict)
    precheck_decision: dict[str, Any] | None = None
    precheck_probe: dict[str, Any] | None = None
    proposal: dict[str, Any] | None = None
    proposal_attempts: list[dict[str, Any]] = field(default_factory=list)
    static_checks: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    remediation_decision: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    applied_version: str | None = None
    recheck_probe: dict[str, Any] | None = None
    recheck_decision: dict[str, Any] | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class Remediator:
    def __init__(self, cfg: AgentConfig, *, store: ConfigStore, prober: Prober, proposer: Proposer, notifier: Notifier,
                 migration_evaluator: pc.Evaluator, remediation_evaluator: pc.Evaluator,
                 decisions: pc.DecisionStore | None = None, records: RemediationStore | None = None) -> None:
        self.cfg = cfg
        self.store = store
        self.prober = prober
        self.proposer = proposer
        self.notifier = notifier
        self.migration_eval = migration_evaluator
        self.remediation_eval = remediation_evaluator
        self.decisions = decisions or pc.DecisionStore(cfg.state_dir)
        self.records = records or RemediationStore(cfg.state_dir)
        self._thread_ts: str | None = None

    # ---- 보고 ------------------------------------------------------------

    def _say(self, rec: Record, text: str) -> None:
        head = f"[{rec.job_id} · {rec.remediation_id}] "
        ts = self.notifier.post(head + text, blocks=[section(head + text)], thread_ts=self._thread_ts)
        if self._thread_ts is None and ts:
            self._thread_ts = ts

    def _save(self, rec: Record, status: str | None = None, *, finished: bool = False) -> None:
        if status:
            rec.status = status
        if finished:
            rec.finished_at = utcnow_iso()
        self.records.save(rec)

    # ---- 판정 ------------------------------------------------------------

    def _precheck(self, rec: Record, snap: Snapshot) -> tuple[pc.Decision, dict[str, Any]]:
        probe = self.prober.probe(snap.values)
        check = probe.as_check(app_id=f"{self.cfg.target_env}:agent-probe")
        checks = {**self.cfg.extra_checks(), "target_app_db": check}
        policy_input = pc.build_input(job_id=rec.job_id, stage=self.cfg.stage, plan=self.cfg.plan, checks=checks)
        decision = pc.gate(self.migration_eval, self.decisions, policy_input)
        return decision, check

    def _remediation_gate(self, rec: Record, proposal: Proposal, verification: dict[str, Any],
                          approval: Mapping[str, Any] | None) -> pc.Decision:
        policy_input = pc.build_input(
            job_id=rec.job_id, stage=REMEDIATION_STAGE, plan=self.cfg.plan, checks={"remediation_probe": verification},
        )
        policy_input["proposal"] = {"digest": proposal.digest, "changes": [c.as_dict() for c in proposal.changes]}
        policy_input["scope"] = {"allowed_keys": list(self.cfg.scope.allowed_keys),
                                 "allowed_values": allowed_values(self.cfg.scope, self.cfg.plan)}
        if approval:
            policy_input["approval"] = dict(approval)
        return pc.gate(self.remediation_eval, self.decisions, policy_input)

    # ---- 실행 ------------------------------------------------------------

    def run(self, *, remediation_id: str | None = None) -> Record:
        rec = Record(remediation_id or f"rem-{uuid.uuid4().hex[:10]}", self.cfg.job_id, self.cfg.target_env)
        self._thread_ts = None
        self._save(rec)
        self._say(rec, f"이전 사전 검사 시작 (대상 {self.cfg.target_env}, 저장소 {self.store.name})")
        try:
            return self._run(rec)
        except (StoreError, pc.PolicyError, ProposerError) as exc:
            rec.error = str(exc)
            self._save(rec, FAILED, finished=True)
            self._say(rec, f":x: 중단: {exc}")
            return rec

    def _run(self, rec: Record) -> Record:
        snap = self.store.read()
        rec.store_location, rec.store_version = snap.location, snap.version
        rec.config_masked = snap.masked(self.cfg.scope.secret_keys)

        decision, check = self._precheck(rec, snap)
        rec.precheck_decision, rec.precheck_probe = decision.as_dict(), check
        if decision.allowed:
            self._save(rec, NO_ACTION, finished=True)
            self._say(rec, ":white_check_mark: 정책 통과. 고칠 것이 없다. 이전을 진행해도 된다.")
            return rec

        codes = set(decision.reason_codes)
        if decision.outcome != pc.DENY or not (codes & FIXABLE_CODES) or (codes - FIXABLE_CODES):
            self._save(rec, HELD, finished=True)
            self._say(rec, f":pause_button: 정책 {decision.outcome} ({', '.join(decision.reason_codes)}). "
                           "AI 가 설정으로 고칠 수 있는 사유가 아니다. 사람이 확인해야 한다.")
            return rec

        self._save(rec, PROPOSING)
        self._say(rec, f":robot_face: AI 수정 시작. 근거: 정책 {', '.join(decision.reason_codes)}, "
                       f"탐침 {check['detail'].get('error', '연결 실패')} (host={check['detail'].get('host')})")
        proposal = self._propose(rec, snap, decision, check)
        if proposal is None:
            return rec

        return self._verify_and_apply(rec, snap, proposal, approval=None)

    def _propose(self, rec: Record, snap: Snapshot, decision: pc.Decision, check: Mapping[str, Any]) -> Proposal | None:
        ctx = ProposalContext(
            job_id=rec.job_id, target_env=self.cfg.target_env, values_for_ai=values_for_ai(snap.values, self.cfg.scope),
            reason_codes=list(decision.reason_codes), violations=list(decision.violations),
            probe_detail=check["detail"], scope=self.cfg.scope, plan=self.cfg.plan,
        )
        last_error: str | None = None
        for attempt in range(1, self.cfg.codex.max_attempts + 1):
            try:
                proposal = self.proposer.propose(ctx)
            except ProposerError as exc:
                last_error = str(exc)
                rec.proposal_attempts.append({"attempt": attempt, "error": last_error})
                self._save(rec)
                log.warning("제안 생성 실패 %d/%d: %s", attempt, self.cfg.codex.max_attempts, exc)
                continue
            rec.proposal_attempts.append({"attempt": attempt, "digest": proposal.digest, "changes": len(proposal.changes)})
            if proposal.cannot_fix_reason and not proposal.changes:
                rec.proposal = proposal.as_dict()
                self._save(rec, PROPOSAL_FAILED, finished=True)
                self._say(rec, f":warning: AI 가 허용 범위 안에서 고칠 수 없다고 판단: {proposal.cannot_fix_reason}")
                return None
            rec.proposal = proposal.as_dict()
            self._save(rec)
            changes = ", ".join(f"{c.key}: {c.old!r} → {c.new!r}" for c in proposal.changes)
            self._say(rec, f":memo: 수정안 ({proposal.proposer}): {changes}\n원인: {proposal.root_cause}")
            return proposal
        rec.error = last_error
        self._save(rec, PROPOSAL_FAILED, finished=True)
        self._say(rec, f":x: 수정안 생성 실패 ({self.cfg.codex.max_attempts}회): {last_error}")
        return None

    def _verify_and_apply(self, rec: Record, snap: Snapshot, proposal: Proposal, *, approval: Mapping[str, Any] | None) -> Record:
        self._save(rec, VERIFYING)
        checks = static_checks(self.cfg.scope, self.cfg.plan, proposal)
        rec.static_checks = checks
        if not all_passed(checks):
            failed = [c["name"] for c in checks if c["status"] != "pass"]
            self._save(rec, HELD, finished=True)
            self._say(rec, f":no_entry: 수정안이 허용 범위를 벗어났다 ({', '.join(failed)}). 적용하지 않는다.")
            return rec

        candidate = proposal.apply_to(snap.values)
        probe = self.prober.probe(candidate)
        verification = probe.as_check(app_id=f"{self.cfg.target_env}:agent-probe", extra={"proposal_digest": proposal.digest})
        rec.verification = verification
        self._save(rec)

        decision = self._remediation_gate(rec, proposal, verification, approval)
        rec.remediation_decision = decision.as_dict()
        if decision.outcome in (pc.HOLD,) and "APPROVAL-REQUIRED" in decision.reason_codes and len(decision.reason_codes) == 1:
            self._save(rec, WAITING_APPROVAL)
            self._say(rec, f":hourglass: 수정 값으로 연결 검증 통과(db_connected={probe.db_connected}). "
                           f"자동 적용이 꺼져 있어 승인이 필요하다: `/migrate approve {rec.remediation_id}`")
            return rec
        if not decision.allowed:
            self._save(rec, HELD if decision.outcome == pc.HOLD else FAILED, finished=True)
            self._say(rec, f":no_entry: 수정안 적용 거절: 정책 {decision.outcome} ({', '.join(decision.reason_codes)}). "
                           f"검증: db_connected={probe.db_connected}, error={probe.error or '-'}")
            return rec

        self._save(rec, APPLYING)
        try:
            new_snap = self.store.write(candidate, expected_version=snap.version)
        except VersionConflict as exc:
            rec.error = str(exc)
            self._save(rec, FAILED, finished=True)
            self._say(rec, f":x: 적용 중단: 저장소가 그 사이 바뀌었다 ({exc}). 처음부터 다시 실행한다.")
            return rec
        rec.applied_version = new_snap.version
        self._save(rec, APPLIED)
        self._say(rec, f":wrench: 적용 완료 → {new_snap.location} (정책 {decision.policy_version}, 승인 {'사람' if approval else '자동'})")

        # 재검사: 저장소에서 다시 읽어 같은 정책 경로로 재판정한다. 적용했다는 사실로 통과 처리하지 않는다.
        snap2 = self.store.read()
        rec.store_version = snap2.version
        decision2, check2 = self._precheck(rec, snap2)
        rec.recheck_decision, rec.recheck_probe = decision2.as_dict(), check2
        if decision2.allowed:
            self._save(rec, RECHECK_PASSED, finished=True)
            self._say(rec, ":white_check_mark: 재검사 통과. 이전을 재개할 수 있다.")
        else:
            self._save(rec, RECHECK_FAILED, finished=True)
            self._say(rec, f":x: 재검사 실패: 정책 {decision2.outcome} ({', '.join(decision2.reason_codes)}). 이전은 계속 보류.")
        return rec

    def approve(self, remediation_id: str, *, approver: str) -> Record:
        """사람 승인 뒤 적용. 저장소 버전과 제안 digest 가 기록과 같을 때만."""
        data = self.records.get(remediation_id)
        if data is None:
            raise StoreError(f"기록이 없다: {remediation_id}")
        if data.get("status") != WAITING_APPROVAL or not data.get("proposal"):
            raise StoreError(f"승인 대기 상태가 아니다: {data.get('status')}")
        rec = Record(**{k: v for k, v in data.items()})
        self._thread_ts = None
        proposal = _proposal_from_dict(data["proposal"])
        snap = self.store.read()
        if snap.version != rec.store_version:
            rec.error = f"저장소가 승인 전에 바뀌었다 ({snap.version} != {rec.store_version})"
            self._save(rec, FAILED, finished=True)
            self._say(rec, f":x: 승인 무효: {rec.error}. 다시 실행한다.")
            return rec
        rec.approval = {"approved": True, "approver": approver, "proposal_digest": proposal.digest, "at": utcnow_iso()}
        self._say(rec, f":bust_in_silhouette: {approver} 승인. 적용을 진행한다.")
        try:
            return self._verify_and_apply(rec, snap, proposal, approval=rec.approval)
        except (StoreError, pc.PolicyError) as exc:
            rec.error = str(exc)
            self._save(rec, FAILED, finished=True)
            self._say(rec, f":x: 중단: {exc}")
            return rec


def _proposal_from_dict(d: Mapping[str, Any]) -> Proposal:
    from .scope import Change

    return Proposal(
        changes=tuple(Change(c["key"], c.get("old", ""), c["new"]) for c in d.get("changes", [])),
        analysis=d.get("analysis", ""), root_cause=d.get("root_cause", ""), impact=d.get("impact", ""),
        verification_plan=d.get("verification_plan", ""), cannot_fix_reason=d.get("cannot_fix_reason"),
        proposer=d.get("proposer", "unknown"), model=d.get("model"),
    )


def build_evaluators(cfg: AgentConfig) -> tuple[pc.Evaluator, pc.Evaluator]:
    """(migration 정책 평가기, remediation 정책 평가기). OPA 서버가 있으면 HTTP, 없으면 로컬 opa CLI."""
    if cfg.opa.url:
        return (pc.OpaHttpEvaluator(cfg.opa.url, timeout=cfg.opa.timeout_seconds),
                pc.OpaHttpEvaluator(cfg.opa.url, timeout=cfg.opa.timeout_seconds, path=REMEDIATION_DECISION_PATH))
    if not cfg.opa.policy_dir:
        raise pc.PolicyError("opa.url 또는 opa.policy_dir 가 필요하다")
    return (pc.OpaCliEvaluator(cfg.opa.policy_dir),
            pc.OpaCliEvaluator(cfg.opa.policy_dir, rego="remediation.rego", query="data.ghost.remediation.decision"))
