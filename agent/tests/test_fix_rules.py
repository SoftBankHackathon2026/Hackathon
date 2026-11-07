from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from agent import probe as probe_mod
from agent.config import ProbeSettings, RestartSettings
from agent.demo_fault import inject, restore
from agent.fakes import GOOD_HOST, FakeKv, FakeProber
from agent.probe import PostgresProber, classify
from agent.proposer import build_prompt
from agent.scope import Change, Proposal, allowed_keys, check_proposal

PLAN = {"host": GOOD_HOST, "port": 5432, "name": "migration_demo_return", "user": "migration_app", "sslmode": "verify-full"}
CURRENT = {"DB_HOST": GOOD_HOST, "DB_PORT": "5432", "DB_NAME": "migration_demo", "DB_USER": "migration_app", "DB_SSLMODE": "verify-full"}


def codes(*changes, target_db=PLAN, current=CURRENT):
    p = Proposal(tuple(Change(k, v) for k, v in changes), "c", "i", "v")
    return [r.code for r in check_proposal(p, current=current, target_db=target_db)]


def test_plan_fields_become_fixable_keys() -> None:
    assert allowed_keys(PLAN) == ["DB_HOST", "DB_NAME", "DB_PORT", "DB_SSLMODE", "DB_USER"]
    assert allowed_keys({"host": GOOD_HOST, "port": 5432}) == ["DB_HOST", "DB_PORT"]


def test_db_name_fix_to_plan_value_passes() -> None:
    assert codes(("DB_NAME", "migration_demo_return")) == []


def test_db_name_other_value_rejected() -> None:
    assert codes(("DB_NAME", "postgres")) == ["S2-VALUE-NOT-IN-PLAN"]


def test_db_name_not_in_plan_is_not_allowed() -> None:
    plan = {"host": GOOD_HOST, "port": 5432}
    assert codes(("DB_NAME", "migration_demo_return"), target_db=plan) == ["S1-KEY-NOT-ALLOWED"]


@pytest.mark.parametrize("key,value", [("DB_SSLMODE", "verify-everything"), ("DB_USER", "app; drop table"), ("DB_NAME", "1abc")])
def test_bad_formats_rejected(key: str, value: str) -> None:
    assert codes((key, value)) == ["S4-FORMAT"]


def test_classify_gives_operator_readable_cause() -> None:
    assert classify("tcp", socket.gaierror("x")) == "DNS: 호스트 이름을 찾지 못함"
    assert classify("tcp", ConnectionRefusedError()) == "TCP: 연결 거부(포트에 받는 곳이 없음)"
    assert classify("login", SimpleNamespace(sqlstate="3D000")) == "DB 가 없음 (SQLSTATE 3D000)"
    assert classify("login", Exception("SSL error: certificate verify failed")) == "TLS: 인증서 또는 SSL 설정 오류"
    assert classify("login", Exception('connection failed: FATAL:  database "sample_app_prod" does not exist')) == "DB 가 없음 (SQLSTATE 3D000)"
    assert classify("login", Exception('FATAL:  role "app" does not exist')) == "DB 사용자가 없음 (SQLSTATE 28000)"
    assert classify("login", Exception('FATAL:  password authentication failed for user "u"')) == "비밀번호 인증 실패 (SQLSTATE 28P01)"


def test_probe_uses_sslmode_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}
    monkeypatch.setattr(probe_mod.socket, "create_connection", lambda a, timeout: SimpleNamespace(close=lambda: None))

    def connect(**kwargs):
        seen.update(kwargs)
        raise probe_mod.psycopg.OperationalError("boom")

    monkeypatch.setattr(probe_mod.psycopg, "connect", connect)
    PostgresProber(ProbeSettings(sslmode="verify-full"), env={}).probe({**CURRENT, "DB_SSLMODE": "require"})
    assert seen["sslmode"] == "require"


def test_prompt_separates_diagnosis_and_lists_plan_keys() -> None:
    reasons = [{"code": "APP-01-TARGET-DB-UNREACHABLE", "message": "연결 실패"}, {"code": "DIAG-LOGIN", "message": "DB 가 없음 (SQLSTATE 3D000)"}]
    prompt = build_prompt(reasons, PLAN)
    opa, diag = prompt.split("[진단]", 1)
    assert "DIAG-LOGIN" not in opa.split("[OPA 거절 사유]", 1)[1] and "SQLSTATE 3D000" in diag
    assert "DB_HOST, DB_NAME, DB_PORT, DB_SSLMODE, DB_USER" in prompt
    assert "migration_demo_return" in prompt


def test_working_config_is_handed_off_without_ai(make_world) -> None:
    from agent.fakes import FakeEvaluator

    w = make_world(kv=FakeKv({"DB_HOST": GOOD_HOST, "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u"}),
                   evaluator=FakeEvaluator(extra_reason="APP-01-ORDER-CHECK-FAILED", force_outcome="deny"))
    run = w.remediator.run()
    assert run.state == "HANDED_OFF" and run.detail["diagnosis"]["ok"] is True
    assert w.proposer.calls == []


def test_diagnosis_reaches_proposer(make_world) -> None:
    w = make_world()
    w.remediator.run()
    reasons = w.proposer.calls[0]["reasons"]
    assert {"code": "DIAG-TCP", "message": "DNS: 호스트 이름을 찾지 못함"} in reasons
    assert w.prober.diagnosed and w.prober.diagnosed[0]["DB_HOST"] != GOOD_HOST


def test_demo_fault_sets_any_visible_key_and_restores_plan() -> None:
    kv = FakeKv(dict(CURRENT))
    cfg = SimpleNamespace(restart=RestartSettings(), target_db=PLAN)

    class Api:
        def get(self, path):
            return {"items": []}

    inject(cfg, kv, Api(), changes={"DB_NAME": "migration_demo"})
    assert kv.data["DB_NAME"] == "migration_demo"
    with pytest.raises(ValueError):
        inject(cfg, kv, Api(), changes={"DB_PASSWORD": "x"})
    restore(cfg, kv, SimpleNamespace(restart_and_wait=lambda: None))
    assert kv.data["DB_NAME"] == "migration_demo_return" and kv.data["DB_SSLMODE"] == "verify-full"
