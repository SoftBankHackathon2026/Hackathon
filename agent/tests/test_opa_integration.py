"""실제 Rego 와의 일치. AGENT_TEST_OPA_URL 이 있을 때만 돈다.

  docker run --rm -d -p 18181:8181 -v "$PWD/../policies:/p:ro" openpolicyagent/opa:1.22.0-static run --server --addr :8181 /p/migration.rego
  AGENT_TEST_OPA_URL=http://127.0.0.1:18181 pytest -m integration
"""

from __future__ import annotations

import os

import pytest

from agent.fakes import GOOD_HOST, FakeKv
from migration.policy_client import OpaHttpEvaluator

pytestmark = pytest.mark.integration
OPA_URL = os.environ.get("AGENT_TEST_OPA_URL")
skip = pytest.mark.skipif(not OPA_URL, reason="AGENT_TEST_OPA_URL 미설정")


@skip
def test_real_policy_deny_then_allow(make_world) -> None:
    w = make_world()
    w.remediator.d.evaluator = OpaHttpEvaluator(OPA_URL)
    run = w.remediator.run()
    assert run.state == "RESOLVED"
    assert set(run.detail["pre_decision"]["reason_codes"]) == {"APP-01-TARGET-DB-UNREACHABLE", "APP-01-ORDER-CHECK-FAILED"}
    assert run.detail["post_decision"] == {**run.detail["post_decision"], "outcome": "allow", "reason_codes": []}


@skip
def test_real_policy_allows_healthy_state(make_world) -> None:
    w = make_world(kv=FakeKv({"DB_HOST": GOOD_HOST, "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u"}))
    w.remediator.d.evaluator = OpaHttpEvaluator(OPA_URL)
    assert w.remediator.run().state == "NOTHING_TO_FIX"


@skip
def test_real_policy_holds_when_replication_never_seen(make_world) -> None:
    """앱은 복구됐어도 복제가 보이지 않으면 재판정은 hold 다. 성공으로 끝나지 않는다."""
    w = make_world()
    w.remediator.d.evaluator = OpaHttpEvaluator(OPA_URL)
    w.apps.replicating = False
    run = w.remediator.run()
    assert run.state == "UNRESOLVED"
    assert run.detail["post_decision"]["outcome"] == "hold"
    assert run.detail["post_decision"]["reason_codes"] == ["APP-01-UNVERIFIED"]
    assert w.kv.version == 2  # 저장소는 고쳐졌고, 그 사실도 보고에 남는다


@skip
def test_real_policy_target_only_deny_then_allow(make_world) -> None:
    """--target-only 증거로도 실제 rego 가 거절하고 허용한다. 복제가 없어도 hold 가 되지 않는다."""
    w = make_world(target_only=True)
    w.apps.replicating = False
    w.remediator.d.evaluator = OpaHttpEvaluator(OPA_URL)
    run = w.remediator.run()
    assert run.state == "RESOLVED"
    assert run.detail["pre_decision"]["outcome"] == "deny"
    assert run.detail["post_decision"]["outcome"] == "allow"
    assert w.apps.rows == {}  # 어디에도 쓰지 않았다
    assert any("복제·쓰기는 검증하지 않았다" in note for note in w.notes)
