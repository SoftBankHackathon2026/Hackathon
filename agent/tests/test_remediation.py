"""전체 흐름. 정책은 실제 Rego 를 로컬 opa 로 평가한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import remediation as rm
from agent.config import parse_config
from agent.scope import Change
from agent.stores import FileStore
from migration import policy_client as pc
from tests.conftest import GOOD_HOST, FakeProber, StaticProposer, raw_config, requires_opa, write_evidence_extra

pytestmark = requires_opa


def make(cfg, *, prober=None, proposer=None, notifier=None):
    mig, rem = rm.build_evaluators(cfg)
    from agent.notify import LogNotifier

    return rm.Remediator(cfg, store=FileStore(cfg.store.path or ""), prober=prober or FakeProber(),
                         proposer=proposer or StaticProposer(), notifier=notifier or LogNotifier(),
                         migration_evaluator=mig, remediation_evaluator=rem)


def test_full_cycle_auto_apply(cfg, notifier):
    r = make(cfg, notifier=notifier)
    rec = r.run()
    assert rec.status == rm.RECHECK_PASSED, rec.as_dict()
    assert rec.precheck_decision["outcome"] == "deny"
    assert "APP-01-TARGET-DB-UNREACHABLE" in rec.precheck_decision["reason_codes"]
    assert rec.remediation_decision["outcome"] == "allow"
    assert rec.recheck_decision["outcome"] == "allow"
    assert json.loads(Path(cfg.store.path).read_text())["DB_HOST"] == GOOD_HOST
    # 비밀값은 어디에도 없어야 한다
    dumped = json.dumps(rec.as_dict(), ensure_ascii=False) + "\n".join(notifier.messages)
    assert "s3cret" not in dumped
    assert rec.config_masked["DB_PASSWORD"] == "***"
    # 보고 순서: 시작 → AI 수정 시작 → 수정안 → 적용 → 재검사
    joined = "\n".join(notifier.messages)
    assert "AI 수정 시작" in joined and "적용 완료" in joined and "재검사 통과" in joined
    # 판정 기록 3건 (pre_cutover deny, remediation allow, pre_cutover allow)
    hist = r.decisions.history("forward_001")
    assert [d.stage for d in hist][::-1] == ["pre_cutover", "remediation", "pre_cutover"]
    assert r.records.latest("forward_001")["status"] == rm.RECHECK_PASSED


def test_no_action_when_already_ok(cfg, target_values, notifier):
    Path(cfg.store.path).write_text(json.dumps({**target_values, "DB_HOST": GOOD_HOST}), encoding="utf-8")
    rec = make(cfg, notifier=notifier).run()
    assert rec.status == rm.NO_ACTION
    assert "고칠 것이 없다" in notifier.messages[-1]


def test_held_when_reason_not_fixable(tmp_path, target_values, notifier):
    """다른 수집기 증거가 없으면 정책이 EVIDENCE-MISSING(hold) → AI 가 손대지 않는다."""
    (tmp_path / "target.json").write_text(json.dumps(target_values), encoding="utf-8")
    cfg = parse_config(raw_config(tmp_path, evidence_extra="none.json"), base_dir=tmp_path)
    rec = make(cfg, notifier=notifier).run()
    assert rec.status == rm.HELD and rec.proposal is None
    assert "사람이 확인" in notifier.messages[-1]


def test_held_when_deny_mixed_with_unfixable(tmp_path, target_values, notifier):
    (tmp_path / "target.json").write_text(json.dumps(target_values), encoding="utf-8")
    checks = {
        "target_db_exposure": pc.build_check("pass", {"public": True}, source="t"),
        "target_location": pc.build_check("pass", {"location": "ap-northeast-2"}, source="t"),
    }
    (tmp_path / "evidence_extra.json").write_text(json.dumps(checks), encoding="utf-8")
    cfg = parse_config(raw_config(tmp_path), base_dir=tmp_path)
    rec = make(cfg, notifier=notifier).run()
    assert rec.status == rm.HELD and rec.proposal is None
    assert "SEC-01-PUBLIC-DB" in rec.precheck_decision["reason_codes"]


def test_out_of_scope_proposal_is_rejected_before_policy(cfg, notifier):
    proposer = StaticProposer([Change("DB_NAME", "migration_demo", "other")])
    rec = make(cfg, proposer=proposer, notifier=notifier).run()
    assert rec.status == rm.HELD and rec.remediation_decision is None
    assert "허용 범위를 벗어났다" in notifier.messages[-1]
    assert json.loads(Path(cfg.store.path).read_text())["DB_NAME"] == "migration_demo"


def test_value_outside_plan_is_rejected(cfg, notifier):
    proposer = StaticProposer([Change("DB_HOST", "wrong.internal", "attacker.example")])
    rec = make(cfg, proposer=proposer, notifier=notifier).run()
    assert rec.status == rm.HELD
    assert json.loads(Path(cfg.store.path).read_text())["DB_HOST"] == "wrong.internal"


def test_probe_fail_after_fix_holds(cfg, notifier):
    """계획의 호스트가 맞아도 실제로 안 붙으면 적용하지 않는다 (REM-04)."""
    prober = FakeProber(good_hosts=())
    rec = make(cfg, prober=prober, notifier=notifier).run()
    assert rec.status == rm.HELD
    assert "REM-04-PROBE-FAILED" in rec.remediation_decision["reason_codes"]
    assert json.loads(Path(cfg.store.path).read_text())["DB_HOST"] == "wrong.internal"


def test_proposer_retry_then_success(cfg, notifier):
    proposer = StaticProposer(errors=["codex 종료 코드 3"])
    rec = make(cfg, proposer=proposer, notifier=notifier).run()
    assert rec.status == rm.RECHECK_PASSED
    assert [a.get("error") for a in rec.proposal_attempts] == ["codex 종료 코드 3", None]


def test_proposer_exhausted(cfg, notifier):
    proposer = StaticProposer(errors=["e1", "e2"])
    rec = make(cfg, proposer=proposer, notifier=notifier).run()
    assert rec.status == rm.PROPOSAL_FAILED and rec.error == "e2"


def test_cannot_fix(cfg, notifier):
    proposer = StaticProposer(changes=[], cannot_fix="허용 값에 맞는 호스트 없음")
    rec = make(cfg, proposer=proposer, notifier=notifier).run()
    assert rec.status == rm.PROPOSAL_FAILED
    assert "고칠 수 없다고 판단" in notifier.messages[-1]


def test_approval_flow(tmp_path, target_values, notifier):
    (tmp_path / "target.json").write_text(json.dumps(target_values), encoding="utf-8")
    write_evidence_extra(tmp_path / "evidence_extra.json")
    raw = raw_config(tmp_path)
    raw["plan"]["remediation"]["auto_apply"] = False
    cfg = parse_config(raw, base_dir=tmp_path)
    r = make(cfg, notifier=notifier)
    rec = r.run()
    assert rec.status == rm.WAITING_APPROVAL
    assert "APPROVAL-REQUIRED" in rec.remediation_decision["reason_codes"]
    assert json.loads(Path(cfg.store.path).read_text())["DB_HOST"] == "wrong.internal"
    assert f"/migrate approve {rec.remediation_id}" in notifier.messages[-1]

    rec2 = r.approve(rec.remediation_id, approver="gayeon")
    assert rec2.status == rm.RECHECK_PASSED
    assert rec2.approval["approver"] == "gayeon"
    assert json.loads(Path(cfg.store.path).read_text())["DB_HOST"] == GOOD_HOST


def test_approval_invalid_if_store_changed(tmp_path, target_values, notifier):
    (tmp_path / "target.json").write_text(json.dumps(target_values), encoding="utf-8")
    write_evidence_extra(tmp_path / "evidence_extra.json")
    raw = raw_config(tmp_path)
    raw["plan"]["remediation"]["auto_apply"] = False
    cfg = parse_config(raw, base_dir=tmp_path)
    r = make(cfg, notifier=notifier)
    rec = r.run()
    Path(cfg.store.path).write_text(json.dumps({**target_values, "DB_PORT": "5433"}), encoding="utf-8")
    rec2 = r.approve(rec.remediation_id, approver="gayeon")
    assert rec2.status == rm.FAILED and "바뀌었다" in (rec2.error or "")


def test_approve_requires_waiting_state(cfg):
    r = make(cfg)
    rec = r.run()
    from agent.stores import StoreError

    with pytest.raises(StoreError, match="승인 대기"):
        r.approve(rec.remediation_id, approver="x")


def test_store_conflict_during_apply(cfg, notifier):
    """정책 통과 뒤 쓰기 직전에 저장소가 바뀌면 적용하지 않는다."""

    class Flaky(FileStore):
        def write(self, values, *, expected_version):
            Path(self.path).write_text(json.dumps({"DB_HOST": "someone-else", "DB_PORT": "5432"}), encoding="utf-8")
            return super().write(values, expected_version=expected_version)

    mig, rem = rm.build_evaluators(cfg)
    from agent.notify import LogNotifier

    r = rm.Remediator(cfg, store=Flaky(cfg.store.path or ""), prober=FakeProber(), proposer=StaticProposer(),
                      notifier=notifier, migration_evaluator=mig, remediation_evaluator=rem)
    rec = r.run()
    assert rec.status == rm.FAILED and "바뀌었다" in (rec.error or "")
