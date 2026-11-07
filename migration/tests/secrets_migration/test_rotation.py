import copy
from datetime import UTC, datetime, timedelta

import pytest
from migration.secrets.common import SecretsError, json_value
from migration.secrets.engine import run
from migration.secrets.models import MigrationError, SecretValue
from migration.secrets.operations.postgres import PostgresTarget
from migration.secrets.rotation import candidate, password, rotate
from migration.secrets.state import State
from migration.secrets.target import decode

from .conftest import ARN, PATH
from .test_postgres_rotation import PgDouble
from .test_postgres_rotation import spec as postgres_spec


@pytest.fixture
def rotation_case(scenario, monkeypatch):
    s = scenario
    monkeypatch.setenv("ROTATION_TEST_ADMIN_PASSWORD", "PRIVATE-ADMIN-CANARY")
    s.source.values["v2"] = SecretValue(b"old", "string")
    s.spec = {
        "product": "postgresql",
        "format": "password_only",
        "postgres": dict(postgres_spec().__dict__),
    }
    s.operation_id = "rotation-one"
    s.ready = {
        "schema_version": 1,
        "plan_hash": s.plan["plan_hash"],
        "source_arn": ARN,
        "operation_id": s.operation_id,
        "prepared_by": "operator-test",
        "issued_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "application_prepared": True,
        "source_rotation_paused": True,
    }
    s.server = PgDouble()
    s.service = PostgresTarget(postgres_spec(), connect=s.server.connect)
    with State(s.state) as state:
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    return s


def invoke(s, *, check_only=False, ready=None):
    with State(s.state) as copied, State(s.directory / "rotation.sqlite3") as state:
        return rotate(
            s.config,
            s.plan,
            s.prepared,
            s.plan["rows"][0],
            s.spec,
            ready or s.ready,
            s.operation_id,
            s.target,
            copied,
            state,
            service=s.service,
            check_only=check_only,
        )


def test_postgres_rotation_and_idempotent_check_use_real_adapter(rotation_case):
    s = rotation_case
    result = invoke(s)
    assert (
        result["rotation_status"] == "verified"
        and result["migration_complete"] is False
    )
    assert s.server.alter_count == 1 and s.server.password != "old"
    assert invoke(s, check_only=True)["rotation_status"] == "verified"
    with State(s.state) as copied:
        with pytest.raises(SecretsError, match="ROTATION_ALREADY_STARTED"):
            run(
                s.config,
                s.plan,
                s.prepared,
                s.source,
                s.target,
                copied,
                verify_only=True,
            )
        assert copied.status()["job"]["stage"] == "verified"
    assert invoke(s, check_only=True)["rotation_status"] == "verified"
    invoke(s)
    assert s.server.alter_count == 1
    doc, _ = s.target.docs[PATH]
    assert decode(doc["values"][doc["previous"]]).data == b"old"
    assert decode(doc["values"][doc["current"]]).data.decode() == s.server.password
    assert b"PRIVATE-ADMIN" not in (s.directory / "rotation.sqlite3").read_bytes()
    assert (
        s.server.password.encode()
        not in (s.directory / "rotation.sqlite3").read_bytes()
    )


def test_json_nested_explicit_field_changes_only_password(rotation_case):
    s = rotation_case
    # Rebuild the copied resource before rotating; no production fixture state is reused.
    doc, version = s.target.docs[PATH]
    original = b'{ "host": "db.test", "credentials": {"password": "old", "user":"app"}, "port":5432 }'
    from migration.secrets.target import encode

    doc["values"]["v2"] = encode(SecretValue(original, "string"))
    s.target.docs[PATH] = (doc, version)
    s.spec.update(format="json", password_pointer="/credentials/password")
    invoke(s)
    doc, _ = s.target.docs[PATH]
    old = json_value(decode(doc["values"][doc["previous"]]).data)
    new = json_value(decode(doc["values"][doc["current"]]).data)
    old["credentials"]["password"] = s.server.password
    assert new == old


@pytest.mark.parametrize("phase", ["candidate", "external", "final"])
def test_crash_recovers_same_candidate_without_duplicate_external_change(
    rotation_case, phase
):
    s = rotation_case
    if phase == "candidate":
        s.target.crash = "after"
    elif phase == "external":
        s.server.crash_after_change = True
    else:
        original = s.target.write_document

        def crash_final(path, doc, version):
            if doc.get("rotation", {}).get("phase") == "complete":
                s.target.crash = "after"
            return original(path, doc, version)

        s.target.write_document = crash_final
    with pytest.raises((SecretsError, MigrationError)):
        invoke(s)
    candidate_doc, _ = s.target.docs[PATH]
    pending = candidate_doc["pending"] or candidate_doc["current"]
    stored_candidate = decode(candidate_doc["values"][pending]).data
    if phase == "final":
        s.target.write_document = original
    assert invoke(s)["rotation_status"] == "verified"
    assert s.server.password.encode() == stored_candidate and s.server.alter_count == 1


def test_unsafe_server_blocks_candidate_and_no_external_mutation(rotation_case):
    s = rotation_case
    s.server.system_id = "999"
    with pytest.raises(MigrationError, match="identity_mismatch"):
        invoke(s)
    assert s.target.writes == 1 and s.server.alter_count == 0


@pytest.mark.parametrize(
    "change",
    [
        {"application_prepared": False},
        {"source_rotation_paused": False},
        {"source_arn": "other"},
        {"plan_hash": "other"},
        {"operation_id": "other"},
        {"expires_at": "2000-01-01T00:00:00Z"},
        {"issued_at": "not-a-time"},
    ],
)
def test_missing_or_expired_app_preparation_blocks(rotation_case, change):
    s = rotation_case
    s.ready.update(change)
    with pytest.raises(SecretsError):
        invoke(s)
    assert s.target.writes == 1 and s.server.alter_count == 0


def test_new_rotation_cannot_bypass_pending_work(rotation_case):
    s = rotation_case
    s.server.crash_after_change = True
    with pytest.raises(MigrationError):
        invoke(s)
    previous = s.server.password
    s.operation_id = "rotation-two"
    s.ready["operation_id"] = s.operation_id
    with pytest.raises(SecretsError, match="UNRESOLVED"):
        invoke(s)
    assert s.server.password == previous and s.server.alter_count == 1


@pytest.mark.parametrize(
    "value,changes",
    [
        (
            b'{"password":"one","password":"two"}',
            {"format": "json", "password_pointer": "/password"},
        ),
        (b'{"password":1}', {"format": "json", "password_pointer": "/password"}),
        (b'{"password":"one"}', {"format": "password_only"}),
        (b'{"password":"one"}', {"format": "json", "password_pointer": "/missing"}),
        (b'{"items":["one"]}', {"format": "json", "password_pointer": "/items/0"}),
    ],
)
def test_malformed_or_ambiguous_json_never_rotates(rotation_case, value, changes):
    s = rotation_case
    spec = copy.deepcopy(s.spec)
    spec.update(changes)
    with pytest.raises(SecretsError):
        password(SecretValue(value, "string"), spec)


def test_json_pointer_escaping_is_explicit(rotation_case):
    spec = {
        **rotation_case.spec,
        "format": "json",
        "password_pointer": "/a~1b/~0password",
    }
    old = SecretValue(b'{"a/b":{"~password":"old"},"same":true}', "string")
    changed = candidate(old, spec)
    assert password(changed, spec).data != b"old"
    assert json_value(changed.data)["same"] is True


def test_changed_spec_does_not_reuse_candidate(rotation_case):
    s = rotation_case
    s.target.crash = "after"
    with pytest.raises(SecretsError):
        invoke(s)
    s.spec["postgres"]["dbname"] = "another_db"
    from migration.secrets.state import StateError

    with pytest.raises(StateError, match="PLAN_CHANGED"):
        invoke(s)
    assert s.server.alter_count == 0


def test_copied_value_that_does_not_log_in_as_role_blocks_rotation(rotation_case):
    s = rotation_case
    s.server.password = "used-by-another-role"
    with pytest.raises(SecretsError, match="CURRENT_CREDENTIAL_NOT_FOR_ROLE"):
        invoke(s)
    assert s.target.writes == 1 and s.server.alter_count == 0
