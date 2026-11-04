"""policy_client: 응답 검증·gate·기록·재사용 규칙. OPA 가 설치돼 있으면 Rego 예시도 실제로 평가한다."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from migration import policy_client as pc

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"
EXAMPLES = POLICY_DIR / "examples"

PLAN: dict[str, Any] = {
    "digest": "sha256:plan-1",
    "source": "onprem",
    "target": "aws",
    "approved_locations": ["ap-northeast-2"],
    "limits": {"evidence_ttl_seconds": 300, "write_wait_max_seconds": 30},
}


def good_input(stage: str = "pre_start") -> dict[str, Any]:
    checks = {
        "target_db_exposure": pc.build_check("pass", {"public": False}, source="evidence.test"),
        "target_location": pc.build_check("pass", {"location": "ap-northeast-2"}, source="evidence.test"),
    }
    return pc.build_input(job_id="forward_001", stage=stage, plan=PLAN, checks=checks, snapshot_id="snap-1")


def good_result(inp: dict[str, Any], **override: Any) -> dict[str, Any]:
    base = {
        "allowed": True, "outcome": "allow", "reason_codes": [], "violations": [],
        "policy_version": "migration-policy-v1", "schema_version": "1",
        "job_id": inp["job_id"], "stage": inp["stage"],
        "plan_digest": inp["plan"]["digest"], "evidence_snapshot_id": inp["evidence"]["snapshot_id"],
        "evaluated_rules": ["SEC-01", "SEC-02"], "required_checks": ["target_db_exposure", "target_location"],
    }
    base.update(override)
    return base


class FakeEvaluator:
    name = "fake"

    def __init__(self, result: Any = None, error: str | None = None) -> None:
        self.result, self.error, self.calls = result, error, []

    def evaluate(self, policy_input):  # type: ignore[no-untyped-def]
        self.calls.append(policy_input)
        if self.error:
            raise pc.PolicyError(self.error)
        return pc.parse_decision_result(self.result), "opa-1"


# ---- 입력 ------------------------------------------------------------------


def test_build_input_shape() -> None:
    inp = good_input()
    assert inp["schema_version"] == "1"
    assert inp["now"].endswith("Z")
    assert inp["evidence"]["plan_digest"] == PLAN["digest"]
    assert pc.validate_input(inp) == []


def test_build_input_rejects_bad_stage_and_missing_digest() -> None:
    with pytest.raises(ValueError):
        pc.build_input(job_id="j", stage="cutover", plan=PLAN, checks={})
    with pytest.raises(ValueError):
        pc.build_input(job_id="j", stage="plan", plan={"limits": {}}, checks={})


def test_validate_input_blocks_secret_like_keys() -> None:
    inp = good_input()
    inp["evidence"]["checks"]["target_db_exposure"]["detail"]["db_password"] = "x"
    assert any("secret" in p for p in pc.validate_input(inp))


def test_build_check_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        pc.build_check("ok", {}, source="s")


def test_digest_is_canonical() -> None:
    assert pc.digest_of({"b": 1, "a": [1, 2]}) == pc.digest_of({"a": [1, 2], "b": 1})


# ---- 응답 검증: 200 이어도 형식이 틀리면 허용 아님 ------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"allowed": True, "outcome": "deny", "reason_codes": ["X"]},  # allowed 와 outcome 불일치
        {"allowed": "true"},  # 문자열 boolean
        {"outcome": "ok"},  # 모르는 outcome
        {"outcome": "allow", "reason_codes": ["SEC-01-PUBLIC-DB"]},  # allow 인데 이유 있음
        {"allowed": False, "outcome": "deny", "reason_codes": []},  # deny 인데 이유 없음
        {"policy_version": ""},
        {"violations": [{"no_code": 1}], "allowed": False, "outcome": "hold", "reason_codes": ["E"]},
    ],
)
def test_parse_decision_result_rejects_malformed(override: dict[str, Any]) -> None:
    with pytest.raises(pc.PolicyError):
        pc.parse_decision_result(good_result(good_input(), **override))


def test_parse_decision_result_rejects_non_object() -> None:
    for bad in (None, True, [], "allow"):
        with pytest.raises(pc.PolicyError):
            pc.parse_decision_result(bad)


# ---- evaluate / gate ---------------------------------------------------------


def test_evaluate_allow(tmp_path: Path) -> None:
    inp = good_input()
    d = pc.evaluate(FakeEvaluator(good_result(inp)), inp)
    assert d.allowed and d.outcome == "allow" and d.reason_codes == ()
    assert d.plan_digest == "sha256:plan-1" and d.evidence_snapshot_id == "snap-1"
    assert d.opa_decision_id == "opa-1"
    pc.require_allowed(d)


def test_evaluate_deny_blocks() -> None:
    inp = good_input()
    res = good_result(inp, allowed=False, outcome="deny", reason_codes=["SEC-01-PUBLIC-DB"],
                      violations=[{"code": "SEC-01-PUBLIC-DB", "rule": "SEC-01", "effect": "deny", "message": "m"}])
    d = pc.evaluate(FakeEvaluator(res), inp)
    assert not d.allowed and d.outcome == "deny"
    with pytest.raises(pc.PolicyBlocked) as ei:
        pc.require_allowed(d)
    assert ei.value.decision is d


def test_evaluate_service_down_is_error_not_allow() -> None:
    d = pc.evaluate(FakeEvaluator(error="OPA 연결 실패/timeout"), good_input())
    assert d.outcome == "error" and not d.allowed
    assert d.reason_codes == (pc.POLICY_SERVICE_UNAVAILABLE,)
    assert d.error


def test_evaluate_malformed_is_error() -> None:
    d = pc.evaluate(FakeEvaluator(error="OPA 응답에 result 가 없다"), good_input())
    assert d.outcome == "error" and d.reason_codes == (pc.POLICY_RESPONSE_MALFORMED,)


def test_evaluate_response_for_other_job_is_error() -> None:
    inp = good_input()
    d = pc.evaluate(FakeEvaluator(good_result(inp, job_id="other")), inp)
    assert d.outcome == "error" and d.reason_codes == (pc.POLICY_RESPONSE_MALFORMED,)


def test_evaluate_invalid_input_never_calls_opa() -> None:
    inp = good_input()
    inp["schema_version"] = "2"
    ev = FakeEvaluator(good_result(inp))
    d = pc.evaluate(ev, inp)
    assert d.outcome == "error" and d.reason_codes == (pc.POLICY_INPUT_INVALID,)
    assert ev.calls == []


def test_gate_records_every_decision(tmp_path: Path) -> None:
    store = pc.DecisionStore(tmp_path)
    inp = good_input()
    d1 = pc.gate(FakeEvaluator(good_result(inp)), store, inp)
    d2 = pc.gate(FakeEvaluator(error="down"), store, inp)
    assert store.latest("forward_001", "pre_start").decision_id == d2.decision_id
    hist = store.history("forward_001")
    assert [d.decision_id for d in hist] == [d2.decision_id, d1.decision_id]
    got = store.get(d1.decision_id)
    assert got is not None and got.allowed and got.policy_version == "migration-policy-v1"
    assert store.input_for(d1.decision_id) == json.loads(pc.canonical_json(inp))
    assert store.get(d2.decision_id).error == "down"
    store.close()


def test_store_survives_reopen(tmp_path: Path) -> None:
    inp = good_input()
    store = pc.DecisionStore(tmp_path)
    d = pc.gate(FakeEvaluator(good_result(inp)), store, inp)
    store.close()
    again = pc.DecisionStore(tmp_path)
    got = again.get(d.decision_id)
    assert got is not None and got == d  # raw_result 는 비교 대상이 아니다 (compare=False)
    assert got.raw_result == d.raw_result
    again.close()


# ---- 재사용 규칙: 계획·증거가 바뀌면 옛 allow 를 쓰지 않는다 ---------------


def test_reusable_only_for_same_plan_and_snapshot() -> None:
    inp = good_input()
    d = pc.evaluate(FakeEvaluator(good_result(inp)), inp)
    assert pc.reusable(d, plan_digest="sha256:plan-1", evidence_snapshot_id="snap-1", max_age_seconds=60)
    assert not pc.reusable(d, plan_digest="sha256:plan-2", evidence_snapshot_id="snap-1", max_age_seconds=60)
    assert not pc.reusable(d, plan_digest="sha256:plan-1", evidence_snapshot_id="snap-2", max_age_seconds=60)
    assert not pc.reusable(None, plan_digest="sha256:plan-1", evidence_snapshot_id="snap-1", max_age_seconds=60)


def test_reusable_expires() -> None:
    inp = good_input()
    d = pc.evaluate(FakeEvaluator(good_result(inp)), inp)
    old = d.__class__(**{**d.__dict__, "evaluated_at": (datetime.now(UTC) - timedelta(seconds=120)).isoformat()})
    assert not pc.reusable(old, plan_digest="sha256:plan-1", evidence_snapshot_id="snap-1", max_age_seconds=60)


def test_denied_decision_is_never_reusable() -> None:
    inp = good_input()
    res = good_result(inp, allowed=False, outcome="hold", reason_codes=["EVIDENCE-STALE"],
                      violations=[{"code": "EVIDENCE-STALE"}])
    d = pc.evaluate(FakeEvaluator(res), inp)
    assert not pc.reusable(d, plan_digest="sha256:plan-1", evidence_snapshot_id="snap-1", max_age_seconds=3600)


# ---- HTTP 클라이언트: 가짜 OPA 서버 ----------------------------------------


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Handler.seen.append({"path": self.path, "body": body})
        inp = body.get("input", {})
        if _Handler.mode == "ok":
            doc = {"result": good_result(inp), "decision_id": "opa-dec-1"}
            payload, code = json.dumps(doc).encode(), 200
        elif _Handler.mode == "empty":  # 정책 미적재: OPA 는 200 + {}
            payload, code = b"{}", 200
        elif _Handler.mode == "notjson":
            payload, code = b"<html>", 200
        else:
            payload, code = b'{"code":"internal_error"}', 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_: Any) -> None:
        pass


@pytest.fixture
def fake_opa():  # type: ignore[no-untyped-def]
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Handler.seen = []
    _Handler.mode = "ok"
    try:
        yield f"http://127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()


def test_http_evaluator_ok(fake_opa: str) -> None:
    inp = good_input()
    d = pc.evaluate(pc.OpaHttpEvaluator(fake_opa, timeout=2), inp)
    assert d.allowed and d.opa_decision_id == "opa-dec-1" and d.evaluator == fake_opa
    assert _Handler.seen[0]["path"] == pc.DECISION_PATH
    assert _Handler.seen[0]["body"] == {"input": inp}


@pytest.mark.parametrize("mode,code", [("empty", pc.POLICY_RESPONSE_MALFORMED), ("notjson", pc.POLICY_SERVICE_UNAVAILABLE),
                                       ("500", pc.POLICY_SERVICE_UNAVAILABLE)])
def test_http_evaluator_failures_block(fake_opa: str, mode: str, code: str) -> None:
    _Handler.mode = mode
    d = pc.evaluate(pc.OpaHttpEvaluator(fake_opa, timeout=2), good_input())
    assert d.outcome == "error" and not d.allowed and d.reason_codes == (code,)


def test_http_evaluator_unreachable() -> None:
    d = pc.evaluate(pc.OpaHttpEvaluator("http://127.0.0.1:9", timeout=1), good_input())
    assert d.outcome == "error" and d.reason_codes == (pc.POLICY_SERVICE_UNAVAILABLE,)


# ---- 실제 Rego (opa 가 있을 때) --------------------------------------------

needs_opa = pytest.mark.skipif(not pc.OpaCliEvaluator.available(), reason="opa CLI 없음")


@needs_opa
@pytest.mark.parametrize("name", sorted(p.stem for p in EXAMPLES.glob("*.json") if p.stem != "expected"))
def test_examples_match_expected(name: str) -> None:
    expected = json.loads((EXAMPLES / "expected.json").read_text(encoding="utf-8"))[name]
    inp = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    ev = pc.OpaCliEvaluator(POLICY_DIR)
    if expected["outcome"] == "invalid":
        # 클라이언트는 계약 위반 입력을 OPA 에 보내지 않는다. Rego 쪽 invalid 판정은 평가기를 직접 불러 확인한다.
        assert pc.evaluate(ev, inp).reason_codes == (pc.POLICY_INPUT_INVALID,)
        result, _ = ev.evaluate(inp)
        assert result["outcome"] == "invalid" and result["allowed"] is False
        assert result["reason_codes"] == expected["reason_codes"]
        return
    d = pc.evaluate(ev, inp)
    assert d.outcome == expected["outcome"], d.error
    assert list(d.reason_codes) == expected["reason_codes"]
    assert d.allowed == (expected["outcome"] == "allow")


@needs_opa
def test_gate_end_to_end_with_rego(tmp_path: Path) -> None:
    """C05: 대상 앱 DB 연결 장애 → deny 기록 → 수정 후 새 증거로 allow. 옛 판정은 재사용 불가."""
    store = pc.DecisionStore(tmp_path)
    ev = pc.OpaCliEvaluator(POLICY_DIR)
    plan = dict(PLAN)
    checks = {
        "target_db_exposure": pc.build_check("pass", {"public": False}, source="evidence.aws"),
        "target_location": pc.build_check("pass", {"location": "ap-northeast-2"}, source="evidence.aws"),
        "target_app_db": pc.build_check("pass", {"db_connected": False, "order_query_ok": False}, source="evidence.app"),
    }
    inp1 = pc.build_input(job_id="forward_001", stage="pre_cutover", plan=plan, checks=checks)
    d1 = pc.gate(ev, store, inp1)
    assert d1.outcome == "deny"
    assert d1.reason_codes == ("APP-01-ORDER-CHECK-FAILED", "APP-01-TARGET-DB-UNREACHABLE")
    with pytest.raises(pc.PolicyBlocked):
        pc.require_allowed(d1)

    checks["target_app_db"] = pc.build_check("pass", {"db_connected": True, "order_query_ok": True}, source="evidence.app")
    inp2 = pc.build_input(job_id="forward_001", stage="pre_cutover", plan=plan, checks=checks)
    assert inp2["evidence"]["snapshot_id"] != inp1["evidence"]["snapshot_id"]
    d2 = pc.gate(ev, store, inp2)
    assert d2.allowed and d2.policy_version == "migration-policy-v1"
    assert not pc.reusable(d1, plan_digest=plan["digest"], evidence_snapshot_id=inp2["evidence"]["snapshot_id"], max_age_seconds=60)
    assert store.latest("forward_001", "pre_cutover").decision_id == d2.decision_id
    store.close()
