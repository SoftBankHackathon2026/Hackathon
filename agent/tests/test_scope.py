from __future__ import annotations

from agent.scope import Change, Proposal, all_passed, allowed_values, diff_values, static_checks
from tests.conftest import GOOD_HOST


def _p(*changes: Change) -> Proposal:
    return Proposal(tuple(changes), "a", "r", "i", "v")


def test_diff_values_detects_add_change_remove():
    before = {"A": "1", "B": "2", "C": "3"}
    after = {"A": "1", "B": "x", "D": "4"}
    assert diff_values(before, after) == (Change("B", "2", "x"), Change("C", "3", ""), Change("D", "", "4"))


def test_digest_depends_only_on_changes():
    a = Proposal((Change("DB_HOST", "x", "y"),), "a1", "r1", "i1", "v1")
    b = Proposal((Change("DB_HOST", "x", "y"),), "a2", "r2", "i2", "v2")
    assert a.digest == b.digest


def test_static_checks_pass(cfg):
    results = static_checks(cfg.scope, cfg.plan, _p(Change("DB_HOST", "wrong", GOOD_HOST)))
    assert all_passed(results), results


def test_static_checks_scope_violation(cfg):
    results = static_checks(cfg.scope, cfg.plan, _p(Change("DB_NAME", "a", "b")))
    assert not all_passed(results)
    assert [r["name"] for r in results if r["status"] == "fail"] == ["S1-scope"]


def test_static_checks_value_violation(cfg):
    results = static_checks(cfg.scope, cfg.plan, _p(Change("DB_HOST", "wrong", "attacker.example")))
    assert [r["name"] for r in results if r["status"] == "fail"] == ["S2-value"]


def test_static_checks_secret_key(cfg):
    results = static_checks(cfg.scope, cfg.plan, _p(Change("DB_PASSWORD", "", "x")))
    failed = {r["name"] for r in results if r["status"] == "fail"}
    assert "S4-secret" in failed and "S1-scope" in failed


def test_static_checks_empty(cfg):
    results = static_checks(cfg.scope, cfg.plan, _p())
    assert [r["name"] for r in results if r["status"] == "fail"] == ["S0-nonempty"]


def test_allowed_values_from_plan(cfg):
    assert allowed_values(cfg.scope, cfg.plan) == {"DB_HOST": [GOOD_HOST], "DB_PORT": ["5432"]}
