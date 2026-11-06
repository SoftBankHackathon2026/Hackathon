"""복구 흐름: OPA allow/deny 분기, 범위 밖 사유, 거절, 탐침 실패, cas 충돌, 승인, 재검증."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent.fakes import GOOD_HOST, FakeEvaluator, FakeKv, FakeProber, FixHostProposer
from agent.proposer import ProposalInvalid, ProposerError
from agent.remediation import fixable
from agent.restart import RestartError
from agent.scope import Change, Proposal
from migration.policy_client import digest_of


class StubProposer:
    def __init__(self, proposal: Proposal | None = None, *, error: Exception | None = None, file_changes: dict | None = None) -> None:
        self.proposal, self.error, self.file_changes, self.calls = proposal, error, file_changes, 0

    def propose(self, **_: Any):
        self.calls += 1
        if self.error:
            raise self.error
        assert self.proposal is not None
        return self.proposal, self.proposal.values() if self.file_changes is None else self.file_changes


def good_kv() -> FakeKv:
    return FakeKv({"DB_HOST": GOOD_HOST, "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u"})


def test_fixed_loop_resolves(make_world) -> None:
    w = make_world()
    run = w.remediator.run()
    assert run.state == "RESOLVED"
    assert run.detail["pre_decision"]["outcome"] == "deny"
    assert "APP-01-TARGET-DB-UNREACHABLE" in run.detail["pre_decision"]["reason_codes"]
    assert run.detail["post_decision"]["outcome"] == "allow"
    assert w.kv.data["DB_HOST"] == GOOD_HOST and w.kv.version == 2
    assert w.restarter.count == 1


def test_allow_reports_nothing_to_fix(make_world) -> None:
    w = make_world(kv=good_kv())
    run = w.remediator.run()
    assert run.state == "NOTHING_TO_FIX"
    assert w.proposer.calls == [] and w.kv.writes == [] and w.restarter.count == 0


def test_http_200_with_non_allow_is_not_allow(make_world) -> None:
    """앱이 멀쩡해도 OPA 가 hold 면 허용이 아니다."""
    w = make_world(kv=good_kv(), evaluator=FakeEvaluator(extra_reason="EVIDENCE-MISSING", force_outcome="hold"))
    assert w.remediator.run().state == "HANDED_OFF"


def test_other_reason_mixed_with_app01_goes_to_human(make_world) -> None:
    w = make_world(evaluator=FakeEvaluator(extra_reason="SEC-01-PUBLIC-DB"))
    run = w.remediator.run()
    assert run.state == "HANDED_OFF"
    assert w.proposer.calls == [] and w.kv.writes == []


def test_hold_on_app01_goes_to_human() -> None:
    from migration.policy_client import Decision

    def decision(outcome: str, codes: tuple[str, ...]) -> Decision:
        return Decision("d", "j", "pre_cutover", outcome, codes, (), "p", "x", "s", "i", "t", "e")

    assert fixable(decision("deny", ("APP-01-TARGET-DB-UNREACHABLE", "APP-01-ORDER-CHECK-FAILED")))
    assert not fixable(decision("hold", ("APP-01-UNVERIFIED",)))
    assert not fixable(decision("deny", ("APP-01-X", "SEC-02-UNAPPROVED-LOCATION")))
    assert not fixable(decision("deny", ()))
    assert not fixable(decision("error", ("POLICY-SERVICE-UNAVAILABLE",)))
    assert not fixable(decision("allow", ()))


def test_opa_unreachable_never_touches_store(make_world) -> None:
    class Down(FakeEvaluator):
        def evaluate(self, policy_input):
            from migration.policy_client import PolicyError
            raise PolicyError("OPA 연결 실패")

    w = make_world(evaluator=Down())
    assert w.remediator.run().state == "HANDED_OFF"
    assert w.kv.writes == []


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ((), "S0-EMPTY"),
        ((Change("DB_NAME", "other"),), "S1-KEY-NOT-ALLOWED"),
        ((Change("DB_PASSWORD", "x"),), "S3-SECRET-KEY"),
        ((Change("DB_HOST", "other.hackathon.svc"),), "S2-VALUE-NOT-IN-PLAN"),
    ],
)
def test_static_rejection_stops_before_probe_and_write(make_world, changes, code) -> None:
    w = make_world(proposer=StubProposer(Proposal(changes, "c", "i", "v")))
    run = w.remediator.run()
    assert run.state == "REJECTED" and code in run.detail["rejections"]
    assert w.prober.seen == [] and w.kv.writes == [] and w.restarter.count == 0


def test_rejected_value_not_recorded_or_reported(make_world) -> None:
    w = make_world(proposer=StubProposer(Proposal((Change("DB_PASSWORD", "hunter2-value"),), "c", "i", "v")))
    run = w.remediator.run()
    assert "hunter2-value" not in json.dumps(run.detail) and "hunter2-value" not in " ".join(w.notes)
    assert "hunter2-value" not in json.dumps(w.runs.events(run.run_id), ensure_ascii=False)


def test_file_mismatch_rejected(make_world) -> None:
    good = Proposal((Change("DB_HOST", GOOD_HOST),), "c", "i", "v")
    w = make_world(proposer=StubProposer(good, file_changes={"DB_HOST": GOOD_HOST, "DB_NAME": "x"}))
    run = w.remediator.run()
    assert run.state == "REJECTED" and run.detail["rejections"] == ["S7-FILE-MISMATCH"]


def test_schema_violation_stops(make_world) -> None:
    w = make_world(proposer=StubProposer(error=ProposalInvalid("출력이 JSON 이 아니다")))
    run = w.remediator.run()
    assert run.state == "REJECTED" and run.detail["rejections"] == ["S-SCHEMA"]
    assert w.kv.writes == []


def test_codex_failure_stops(make_world) -> None:
    w = make_world(proposer=StubProposer(error=ProposerError("codex 시간 초과")))
    assert w.remediator.run().state == "REJECTED" and w.kv.writes == []


def test_probe_failure_stops_before_write(make_world) -> None:
    w = make_world(prober=FakeProber(fail=True))
    run = w.remediator.run()
    assert run.state == "PROBE_FAILED"
    assert w.kv.writes == [] and w.restarter.count == 0


def test_probe_sees_proposed_value_not_current(make_world) -> None:
    w = make_world()
    w.remediator.run()
    assert w.prober.seen[0]["DB_HOST"] == GOOD_HOST and w.prober.seen[0]["DB_NAME"] == "migration_demo"


def test_cas_conflict_stops_before_restart(make_world) -> None:
    w = make_world()

    class Racing(FixHostProposer):
        def propose(self, **kwargs):
            result = super().propose(**kwargs)
            w.kv.version += 1  # 읽은 뒤 다른 사람이 값을 바꿨다
            return result

    w.remediator.d.proposer = Racing()
    run = w.remediator.run()
    assert run.state == "CAS_CONFLICT"
    assert w.kv.writes == [] and w.restarter.count == 0


def test_restart_failure_is_not_success(make_world) -> None:
    w = make_world()

    class Broken:
        def restart_and_wait(self) -> None:
            raise RestartError("Ready 대기 시간 초과")

    w.remediator.d.restarter = Broken()
    run = w.remediator.run()
    assert run.state == "RESTART_FAILED" and "post_decision" not in run.detail


def test_applied_but_verification_fails_is_unresolved(make_world) -> None:
    """저장소에 잘 썼어도 재판정이 allow 가 아니면 성공이 아니다."""
    w = make_world()

    class NoReload:
        count = 0

        def restart_and_wait(self) -> None:
            self.count += 1  # 재시작했다고 하지만 앱은 옛 값을 그대로 쓴다

    w.remediator.d.restarter = NoReload()
    run = w.remediator.run()
    assert run.state == "UNRESOLVED"
    assert w.kv.version == 2 and run.detail["post_decision"]["outcome"] == "deny"


def test_no_auto_apply_waits_before_write_then_approve_resolves(make_world) -> None:
    w = make_world(auto_apply=False)
    run = w.remediator.run()
    assert run.state == "WAITING_APPROVAL"
    assert w.kv.writes == [] and w.restarter.count == 0
    assert f"python -m agent approve {run.run_id}" in " ".join(w.notes)

    approved = w.remediator.approve(run.run_id)
    assert approved.state == "RESOLVED" and w.kv.version == 2


def test_approve_twice_applies_once(make_world) -> None:
    w = make_world(auto_apply=False)
    run = w.remediator.run()
    w.remediator.approve(run.run_id)
    with pytest.raises(ValueError):
        w.remediator.approve(run.run_id)
    assert len(w.kv.writes) == 1


def test_approve_unknown_run_raises(make_world) -> None:
    with pytest.raises(ValueError):
        make_world().remediator.approve("run-none")


def test_approve_after_store_changed_is_conflict(make_world) -> None:
    w = make_world(auto_apply=False)
    run = w.remediator.run()
    w.kv.version += 1
    assert w.remediator.approve(run.run_id).state == "CAS_CONFLICT"
    assert w.kv.writes == []


def test_approve_reprobes(make_world) -> None:
    w = make_world(auto_apply=False)
    run = w.remediator.run()
    w.prober.fail = True
    assert w.remediator.approve(run.run_id).state == "PROBE_FAILED"
    assert w.kv.writes == []


def test_unexpected_exception_is_recorded(make_world) -> None:
    w = make_world()

    class Boom:
        def read(self):
            raise ZeroDivisionError

    w.remediator.d.kv = Boom()
    assert w.remediator.run().state == "ERROR"


def test_run_history_persisted(make_world) -> None:
    w = make_world()
    run = w.remediator.run()
    assert w.runs.get(run.run_id).state == "RESOLVED"
    assert [e["state"] for e in w.runs.events(run.run_id)][0] == "CHECKING"
    assert w.decisions.history("fake_001")[0].allowed is True


def test_decisions_bound_to_plan_digest(make_world) -> None:
    w = make_world()
    w.remediator.run()
    plan = {k: v for k, v in w.remediator.plan.items() if k != "digest"}
    assert w.decisions.latest("fake_001", "pre_cutover").plan_digest == digest_of(plan)
