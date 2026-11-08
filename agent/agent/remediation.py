"""복구 흐름. 스모크 → OPA → (APP-01 거절만) Codex 제안 → 정적 검사 → 탐침 → cas 쓰기 → 재시작 → 재검증.

성공은 재판정 allow 로만 정한다. 쓰기에 성공한 것만으로 성공 처리하지 않는다.
OPA 판정은 migration.policy_client 그대로 쓴다 (HTTP 200 이 아니라 outcome == allow).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from migration.policy_client import DecisionStore, Decision, Evaluator, build_check, build_input, digest_of, gate
from migration.smoke import CHECK_NAME, TARGET_ONLY_MODE

from . import runs as R
from .hypotheses import build_hypotheses
from .kv import CasConflict, KvError, KvStore
from .notify import Notifier
from .probe import Prober
from .proposer import ProposalInvalid, Proposer, ProposerError, visible_config
from .restart import Restarter, RestartError
from .runs import Run, RunStore
from .scope import Proposal, check_proposal, planned_values

log = logging.getLogger("agent.remediation")

APP01_PREFIX = "APP-01-"
DECLARED_SOURCE = "agent.declared-config"
RECOVERED = "RECOVERED"
TITLES: dict[str, str] = {
    R.RESOLVED: "✅ *복구 완료*",
    R.UNRESOLVED: "⚠️ *적용했지만 재검증 실패*",
    R.REJECTED: "🚫 *AI 제안 거절*",
    R.PROBE_FAILED: "❌ *수정 값으로 DB 접속 실패*",
    R.CAS_CONFLICT: "⚠️ *저장소 값이 그사이 바뀜*",
    R.RESTART_FAILED: "❌ *재시작 실패*",
    R.HANDED_OFF: "🙋 *사람 확인 필요*",
    RECOVERED: "🟢 *다시 정상*",
    R.ERROR: "❌ *오류*",
    R.NOTHING_TO_FIX: "정상",
}
QUIET_STATES = frozenset({R.NOTHING_TO_FIX})
STREAK_STATES = frozenset({R.HANDED_OFF, R.REPEATED})


UNVERIFIED_ALERT_AFTER = 3
SLACK_SECTION_LIMIT = 2900


def unverified(decision: Decision) -> bool:
    return decision.outcome == "hold" and bool(decision.reason_codes) and all(
        code.endswith("-UNVERIFIED") for code in decision.reason_codes
    )


def fixable(decision: Decision) -> bool:
    """AI 에게 넘겨도 되는 판정인가. deny 이고 사유가 전부 APP-01-* 일 때만이다. hold·error 는 사람 몫이다."""
    return decision.outcome == "deny" and bool(decision.reason_codes) and all(
        code.startswith(APP01_PREFIX) for code in decision.reason_codes
    )


@dataclass
class Deps:
    smoke: Callable[[], Mapping[str, Any]]
    evaluator: Evaluator
    decisions: DecisionStore
    kv: KvStore
    proposer: Proposer
    prober: Prober
    restarter: Restarter
    notifier: Notifier
    runs: RunStore


class Remediator:
    def __init__(
        self, deps: Deps, *, job_id: str, plan: Mapping[str, Any],
        declared_evidence: Mapping[str, Mapping[str, Any]], auto_apply: bool, stage: str = "pre_cutover",
        approve_command: str = "python -m agent approve", approve_url: str | None = None,
        slack_buttons: bool = False,
    ) -> None:
        self.d = deps
        self.approve_command = approve_command
        self.approve_url = approve_url
        self.slack_buttons = slack_buttons
        self.job_id, self.auto_apply, self.stage = job_id, auto_apply, stage
        self.declared_evidence = declared_evidence
        self.plan = {**plan, "digest": digest_of(dict(plan))}
        self.target_db = plan["target_db"]
        self._smoke_mode: str | None = None

    # ------------------------------------------------------------------
    # 판정
    # ------------------------------------------------------------------

    def _checks(self) -> dict[str, Mapping[str, Any]]:
        """SEC-01·SEC-02 수집기가 아직 없어 설정에 선언한 사실을 쓴다. source 로 선언임을 드러낸다."""
        checks = {
            name: build_check("pass", {**detail, "declared": True}, source=DECLARED_SOURCE)
            for name, detail in self.declared_evidence.items()
        }
        checks[CHECK_NAME] = smoke = self.d.smoke()
        self._smoke_mode = smoke.get("detail", {}).get("mode")
        return checks

    def _judge(self) -> Decision:
        policy_input = build_input(job_id=self.job_id, stage=self.stage, plan=self.plan, checks=self._checks())
        return gate(self.d.evaluator, self.d.decisions, policy_input)

    # ------------------------------------------------------------------
    # 진입점
    # ------------------------------------------------------------------

    def run(self) -> Run:
        run = self.d.runs.create(self.job_id)
        try:
            self._run(run)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 기록하고 사람에게 넘긴다. 문구 대신 종류만 남긴다
            log.exception("실행 %s 실패", run.run_id)
            self._finish(run, R.ERROR, f"예기치 않은 오류 {type(exc).__name__}. 사람이 확인한다")
        return run

    def reject(self, run_id: str, by: str | None = None) -> Run:
        run = self.d.runs.get(run_id)
        if run is None:
            raise ValueError(f"실행 기록이 없다: {run_id}")
        if not self.d.runs.claim_rejection(run_id, by):
            raise ValueError(f"승인 대기 상태가 아니다: {run_id} ({run.state})")
        rejected = self.d.runs.get(run_id) or run
        who = f"{by} 님이 " if by else ""
        self._notify(rejected, f"{TITLES[R.REJECTED]}  `{run_id}`\n{who}제안을 거절했어요. 저장소는 바꾸지 않았어요")
        return rejected

    def approve(self, run_id: str, by: str | None = None) -> Run:
        run = self.d.runs.get(run_id)
        if run is None:
            raise ValueError(f"실행 기록이 없다: {run_id}")
        if not self.d.runs.claim_approval(run_id, by):
            raise ValueError(f"승인 대기 상태가 아니다: {run_id} ({run.state})")
        run.state = R.APPLYING
        try:
            self._approved(run)
        except Exception as exc:  # noqa: BLE001 - run() 과 같다
            log.exception("실행 %s 승인 처리 실패", run.run_id)
            self._finish(run, R.ERROR, f"예기치 않은 오류 {type(exc).__name__}. 사람이 확인한다")
        return run

    # ------------------------------------------------------------------
    # 단계
    # ------------------------------------------------------------------

    def _run(self, run: Run) -> None:
        decision = self._judge()
        run.detail.update(pre_decision=_decision_summary(decision))
        if decision.allowed:
            self._finish(run, R.NOTHING_TO_FIX, "OPA allow. 고칠 것 없음")
            return
        if not fixable(decision):
            self._finish(run, R.HANDED_OFF, f"AI 범위 밖 ({decision.outcome}: {_codes(decision)}). 사람에게 넘긴다",
                         alert_after=UNVERIFIED_ALERT_AFTER if unverified(decision) else 1)
            return

        self._notify(run, f"🔎 *AI 수정 시작*  `{run.run_id}`\n• OPA 판정: *{decision.outcome}*\n• 사유: {_code_list(decision)}")
        try:
            snapshot = self.d.kv.read()
        except KvError as exc:
            self._finish(run, R.ERROR, f"설정 저장소를 읽지 못했다: {exc}")
            return
        diagnosis = self._diagnose(snapshot.data)
        run.detail["diagnosis"] = {"ok": diagnosis.ok, "step": diagnosis.step, "detail": diagnosis.detail}
        run.detail["hypotheses"] = build_hypotheses(
            diagnosis, visible_config(snapshot.data), planned_values(self.target_db),
        )
        if diagnosis.ok:
            self._finish(run, R.HANDED_OFF, "지금 설정으로 DB 에 접속된다. 설정 문제가 아닐 수 있어 사람에게 넘긴다")
            return
        reasons = [dict(v) for v in decision.violations]
        reasons.append({"code": f"DIAG-{diagnosis.step.upper()}", "message": diagnosis.detail or diagnosis.error or "-"})
        try:
            proposal, file_changes = self.d.proposer.propose(
                current=snapshot.data,
                reasons=reasons,
                target_db=self.target_db,
            )
        except (ProposalInvalid, ProposerError) as exc:
            code = "S-SCHEMA" if isinstance(exc, ProposalInvalid) else "CODEX-FAILED"
            self._record_sandbox(run, with_file=False)
            self._finish(run, R.REJECTED, f"제안을 받지 못했다 ({code}): {exc}", rejections=[code])
            return

        rejections = check_proposal(proposal, current=snapshot.data, target_db=self.target_db, file_changes=file_changes)
        if rejections:
            codes = [r.code for r in rejections]
            # 거절된 제안은 값을 저장·보고하지 않는다. 허용 키 밖의 값은 비밀값일 수 있다.
            self._record_sandbox(run, with_file=False)
            self._finish(run, R.REJECTED, "정적 검사 거절: " + "; ".join(r.message for r in rejections), rejections=codes)
            return
        self._record_sandbox(run, with_file=True)

        recorded = proposal.as_dict()
        visible = visible_config(snapshot.data)
        recorded["changes"] = [{**c, "before": visible.get(c["key"])} for c in recorded["changes"]]
        run.detail.update(proposal=recorded, expected_version=snapshot.version)
        if not self._probe(run, proposal, snapshot.data):
            return
        origin = self._open_streak(run)
        if origin is not None:
            self._close_streak(origin, run)
        if not self.auto_apply:
            self.d.runs.update(run, R.WAITING_APPROVAL, "승인 대기")
            head = f"🟡 *승인 대기*  `{run.run_id}`\n{_describe(proposal, snapshot.data)}"
            blocks = self._approval_blocks(run.run_id, head, proposal) if self.slack_buttons else None
            self._notify(run, f"{head}\n*승인*  {self._approve_hint(run.run_id)}", blocks)
            return
        self.d.runs.update(run, R.APPLYING, "자동 적용")
        self._apply(run, proposal, snapshot.version)

    def _approved(self, run: Run) -> None:
        """승인을 기다리는 동안 세상이 바뀌었을 수 있어 정적 검사와 탐침을 다시 한다."""
        proposal = Proposal.from_dict(run.detail["proposal"])
        expected = int(run.detail["expected_version"])
        try:
            snapshot = self.d.kv.read()
        except KvError as exc:
            self._finish(run, R.ERROR, f"설정 저장소를 읽지 못했다: {exc}")
            return
        if snapshot.version != expected:
            self._finish(run, R.CAS_CONFLICT, f"승인 대기 중 값이 바뀌었다 (읽은 버전 {expected}, 현재 {snapshot.version}). 다시 실행한다")
            return
        rejections = check_proposal(proposal, current=snapshot.data, target_db=self.target_db)
        if rejections:
            self._finish(run, R.REJECTED, "승인 시 재검사 거절: " + "; ".join(r.message for r in rejections),
                         rejections=[r.code for r in rejections])
            return
        if self._probe(run, proposal, snapshot.data):
            self._apply(run, proposal, expected)

    def _diagnose(self, current: Mapping[str, Any]):
        diagnose = getattr(self.d.prober, "diagnose", self.d.prober.probe)
        return diagnose(visible_config(current))

    def _probe(self, run: Run, proposal: Proposal, current: Mapping[str, Any]) -> bool:
        result = self.d.prober.probe({**visible_config(current), **proposal.values()})
        run.detail["probe"] = {"ok": result.ok, "step": result.step, "error": result.error}
        if not result.ok:
            self._finish(run, R.PROBE_FAILED, f"수정 값 탐침 실패 ({result.step}: {result.error}). 저장소는 그대로다")
        return result.ok

    def _apply(self, run: Run, proposal: Proposal, expected_version: int) -> None:
        try:
            new_version = self.d.kv.write_changes(proposal.values(), expected_version=expected_version)
        except CasConflict as exc:
            self._finish(run, R.CAS_CONFLICT, f"쓰기 중단: {exc}. 저장소는 그대로다")
            return
        except KvError as exc:
            self._finish(run, R.ERROR, f"설정 저장소 쓰기 실패: {exc}")
            return
        self.d.runs.update(run, R.APPLYING, f"저장소 반영 v{expected_version} → v{new_version}", applied_version=new_version)
        self._notify(run, f"🛠 *저장소 반영*  `{run.run_id}`\n• OpenBao v{expected_version} → v{new_version}\n• 대상 앱을 재시작하고 다시 판정한다")

        try:
            self.d.restarter.restart_and_wait()
        except RestartError as exc:
            self._finish(run, R.RESTART_FAILED, f"저장소는 v{new_version} 으로 바뀌었지만 재시작 실패: {exc}. 사람이 확인한다")
            return

        decision = self._judge()
        run.detail.update(post_decision=_decision_summary(decision))
        if decision.allowed:
            scope = " 대상 앱 DB 조회만 검증했다. 복제·쓰기는 검증하지 않았다." if self._smoke_mode == TARGET_ONLY_MODE else ""
            self._finish(run, R.RESOLVED, f"복구 완료. 재검증 OPA allow (저장소 v{new_version}).{scope}")
        else:
            self._finish(run, R.UNRESOLVED,
                         f"적용했지만 재검증 통과 못함 ({decision.outcome}: {_codes(decision)}). 저장소는 v{new_version} 이다. 사람이 확인한다")

    # ------------------------------------------------------------------
    # 기록·보고
    # ------------------------------------------------------------------

    def _finish(self, run: Run, state: str, message: str, *, alert_after: int = 1, **detail: Any) -> None:
        origin = self._open_streak(run)
        if state == R.HANDED_OFF and origin is not None and origin.detail.get("handoff_message") == message:
            self._repeat(run, origin, message, alert_after)
            return
        if origin is not None:
            self._close_streak(origin, run)
        if state == R.HANDED_OFF:
            detail.update(handoff_message=message, repeats=1, alerted=alert_after == 1)
        self.d.runs.update(run, state, message, **detail)
        if state in QUIET_STATES:
            log.info("[%s] %s", run.run_id, message)
            return
        if state == R.HANDED_OFF and alert_after > 1:
            log.info("[%s] 같은 사유 1번째. %d번 이어지면 알린다: %s", run.run_id, alert_after, message)
            return
        self._notify(run, f"{TITLES.get(state, state)}  `{run.run_id}`\n{message}")

    def _open_streak(self, run: Run) -> Run | None:
        """바로 앞 실행이 아직 끝나지 않은 넘김(또는 그 반복)이면 그 첫 실행을 돌려준다."""
        before = next((r for r in self.d.runs.recent(2) if r.run_id != run.run_id), None)
        if before is None or before.state not in STREAK_STATES:
            return None
        origin = self.d.runs.get(str(before.detail.get("repeat_of"))) if before.state == R.REPEATED else before
        if origin is None or origin.state != R.HANDED_OFF:
            return None
        return origin

    def _repeat(self, run: Run, origin: Run, message: str, alert_after: int) -> None:
        repeats = int(origin.detail.get("repeats") or 1) + 1
        self.d.runs.update(run, R.REPEATED, message, repeat_of=origin.run_id, handoff_message=message)
        self.d.runs.update(origin, R.HANDED_OFF, f"같은 사유로 다시 넘김 ({repeats}번째)", repeats=repeats, last_seen=run.updated_at)
        if not origin.detail.get("alerted") and repeats >= alert_after:
            self.d.runs.update(origin, R.HANDED_OFF, f"같은 사유가 {repeats}번 이어져 알림을 보냄", alerted=True)
            self._notify(origin, f"{TITLES[R.HANDED_OFF]}  `{origin.run_id}`\n{message} (같은 사유 {repeats}번째)")
            return
        log.info("[%s] %s 의 같은 사유 %d번째. 기록은 첫 실행에 합친다", run.run_id, origin.run_id, repeats)

    def _close_streak(self, origin: Run, run: Run) -> None:
        repeats = int(origin.detail.get("repeats") or 1)
        minutes = _minutes_between(origin.created_at, run.updated_at)
        note = f"다시 정상이에요. 같은 사유로 {repeats}번 넘겼고 약 {minutes}분 이어졌어요."
        self.d.runs.update(origin, R.HANDED_OFF, note, closed_at=run.updated_at, closed_by=run.run_id)
        if origin.detail.get("alerted"):
            self._notify(origin, f"{TITLES[RECOVERED]}  `{origin.run_id}`\n{note}")
        else:
            log.info("[%s] %s", origin.run_id, note)

    def _record_sandbox(self, run: Run, *, with_file: bool) -> None:
        trace = getattr(self.d.proposer, "last_trace", None)
        if not isinstance(trace, dict):
            return
        run.detail["sandbox"] = trace if with_file else {k: v for k, v in trace.items() if k != "file"}

    def _approval_blocks(self, run_id: str, head: str, proposal: Proposal) -> list[dict[str, Any]]:
        keys = ", ".join(c.key for c in proposal.changes) or "설정"
        buttons: list[dict[str, Any]] = [
            {
                "type": "button", "action_id": "agent_approve", "value": run_id, "style": "primary",
                "text": {"type": "plain_text", "text": "승인"},
                "confirm": {
                    "title": {"type": "plain_text", "text": "승인할까요?"},
                    "text": {"type": "plain_text", "text": f"{keys} 값을 설정 저장소에 쓰고 앱을 다시 시작해요."[:300]},
                    "confirm": {"type": "plain_text", "text": "승인"},
                    "deny": {"type": "plain_text", "text": "취소"},
                },
            },
            {
                "type": "button", "action_id": "agent_reject", "value": run_id, "style": "danger",
                "text": {"type": "plain_text", "text": "거절"},
            },
        ]
        if self.approve_url:
            buttons.append({
                "type": "button", "action_id": "agent_open",
                "url": f"{self.approve_url.rstrip('/')}/#agent/{run_id}",
                "text": {"type": "plain_text", "text": "플랫폼에서 보기"},
            })
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": head[:SLACK_SECTION_LIMIT]}},
            {"type": "actions", "block_id": "agent_decision", "elements": buttons},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"터미널에서는 `{self.approve_command} {run_id}`"},
            ]},
        ]

    def _approve_hint(self, run_id: str) -> str:
        command = f"`{self.approve_command} {run_id}`"
        if not self.approve_url:
            return command
        return f"<{self.approve_url.rstrip('/')}/#agent/{run_id}|플랫폼에서 승인> 또는 {command}"

    def _notify(self, run: Run, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        if blocks:
            self.d.notifier.send(text, blocks)
        else:
            self.d.notifier.send(text)


def _minutes_between(start: str, end: str) -> int:
    try:
        seconds = (datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    except ValueError:
        return 0
    return max(1, round(seconds / 60))


def _codes(decision: Decision) -> str:
    return ", ".join(decision.reason_codes) or "-"


def _decision_summary(decision: Decision) -> dict[str, Any]:
    return {"decision_id": decision.decision_id, "outcome": decision.outcome, "reason_codes": list(decision.reason_codes)}


def _code_list(decision: Decision) -> str:
    return ", ".join(f"`{code}`" for code in decision.reason_codes) or "-"


def _describe(proposal: Proposal, current: Mapping[str, Any] | None = None) -> str:
    before = current or {}
    changes = "\n".join(
        f"• `{c.key}`  `{before.get(c.key, '-')}` → `{c.value}`" for c in proposal.changes
    )
    return f"*변경*\n{changes}\n*원인*  {proposal.cause}\n*영향*  {proposal.impact}\n*검증*  {proposal.verification}"
