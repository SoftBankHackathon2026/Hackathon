"""정적 검사 거부 케이스."""

from __future__ import annotations

import pytest

from agent.scope import Change, Proposal, check_proposal

TARGET_DB = {"host": "postgres.hackathon.svc.cluster.local", "port": 5432}
CURRENT = {"DB_HOST": "wrong.hackathon.svc", "DB_PORT": "5432", "DB_NAME": "migration_demo", "DB_USER": "migration_app"}


def proposal(*pairs: tuple[str, str]) -> Proposal:
    return Proposal(tuple(Change(k, v) for k, v in pairs), "원인", "영향", "검증")


def codes(p: Proposal, **kwargs) -> list[str]:
    return [r.code for r in check_proposal(p, current=kwargs.pop("current", CURRENT), target_db=TARGET_DB, **kwargs)]


def test_plan_value_passes() -> None:
    assert codes(proposal(("DB_HOST", TARGET_DB["host"]))) == []


def test_host_and_port_together_pass() -> None:
    current = {**CURRENT, "DB_PORT": "6543"}
    assert codes(proposal(("DB_HOST", TARGET_DB["host"]), ("DB_PORT", "5432")), current=current) == []


def test_empty_rejected() -> None:
    assert codes(proposal()) == ["S0-EMPTY"]


@pytest.mark.parametrize("key", ["DB_NAME", "DB_USER", "REQUEST_API_AUTH", "PGSSLMODE"])
def test_key_outside_allowlist_rejected(key: str) -> None:
    assert "S1-KEY-NOT-ALLOWED" in codes(proposal((key, "x")))


@pytest.mark.parametrize("key", ["DB_PASSWORD", "db_passwd", "API_TOKEN", "CLIENT_SECRET", "DATABASE_DSN", "PRIVATE_KEY"])
def test_secret_key_rejected(key: str) -> None:
    assert codes(proposal((key, "x"))) == ["S3-SECRET-KEY"]


def test_rejection_message_does_not_echo_value() -> None:
    result = check_proposal(proposal(("DB_PASSWORD", "hunter2-value")), current=CURRENT, target_db=TARGET_DB)
    assert all("hunter2-value" not in r.message for r in result)


@pytest.mark.parametrize("host", ["other-db.hackathon.svc.cluster.local", "postgres", "10.0.0.1"])
def test_host_not_in_plan_rejected(host: str) -> None:
    assert codes(proposal(("DB_HOST", host))) == ["S2-VALUE-NOT-IN-PLAN"]


def test_port_not_in_plan_rejected() -> None:
    assert codes(proposal(("DB_PORT", "6543"))) == ["S2-VALUE-NOT-IN-PLAN"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DB_HOST", ""),
        ("DB_HOST", "Postgres.Example"),
        ("DB_HOST", "postgres.svc; rm -rf /"),
        ("DB_HOST", "postgres.svc\n"),
        ("DB_HOST", "user:pw@postgres.svc"),
        ("DB_HOST", "-postgres.svc"),
        ("DB_PORT", "abc"),
        ("DB_PORT", "0"),
        ("DB_PORT", "65536"),
        ("DB_PORT", "05432"),
        ("DB_PORT", " 5432"),
    ],
)
def test_bad_format_rejected(key: str, value: str) -> None:
    assert codes(proposal((key, value))) == ["S4-FORMAT"]


def test_same_as_current_rejected() -> None:
    current = {**CURRENT, "DB_HOST": TARGET_DB["host"]}
    assert codes(proposal(("DB_HOST", TARGET_DB["host"])), current=current) == ["S5-NO-CHANGE"]


def test_duplicate_key_rejected() -> None:
    assert "S6-DUPLICATE-KEY" in codes(proposal(("DB_HOST", TARGET_DB["host"]), ("DB_HOST", TARGET_DB["host"])))


def test_file_edit_differs_from_report_rejected() -> None:
    p = proposal(("DB_HOST", TARGET_DB["host"]))
    assert codes(p, file_changes={"DB_HOST": TARGET_DB["host"], "DB_NAME": "other"}) == ["S7-FILE-MISMATCH"]
    assert codes(p, file_changes={"DB_HOST": TARGET_DB["host"]}) == []


def test_one_bad_change_rejects_whole_proposal() -> None:
    assert "S1-KEY-NOT-ALLOWED" in codes(proposal(("DB_HOST", TARGET_DB["host"]), ("DB_NAME", "x")))
