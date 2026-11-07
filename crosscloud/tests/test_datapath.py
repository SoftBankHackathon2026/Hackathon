import dataclasses
import types

import psycopg
import pytest
from memory_secret_store import MemorySecretStore

from crosscloud import datapath, pg_client

_ENDPOINT = pg_client.DbEndpoint(host="db.invalid", port=5432, database="shop")


def _logins(*purposes):
    return tuple(
        datapath.DbLogin(purpose=p, name=f"migration_{p}", secret_id=f"s-{p}")
        for p in purposes
    )


def _plan(side="target", purposes=("admin", "app", "sink", "verify"), **kw):
    fields = {
        "direction": "aws_to_gcp",
        "side": side,
        "endpoint": _ENDPOINT,
        "logins": _logins(*purposes),
        "tables": ("public.orders",),
        "run_id": "ccx-test",
    }
    fields.update(kw)
    return datapath.PrincipalPlan(**fields)


@pytest.mark.parametrize(
    ("direction", "side", "cloud"),
    [
        ("aws_to_gcp", "source", "aws"),
        ("aws_to_gcp", "target", "gcp"),
        ("gcp_to_aws", "source", "gcp"),
        ("gcp_to_aws", "target", "aws"),
    ],
)
def test_cloud_follows_direction_and_side(direction, side, cloud):
    purposes = ("admin", "cdc", "verify") if side == "source" else None
    plan = _plan(
        side=side,
        direction=direction,
        **({"purposes": purposes} if purposes else {}),
    )

    assert plan.cloud == cloud


def test_target_requires_app_and_sink():
    with pytest.raises(ValueError, match="target"):
        _plan(purposes=("admin", "cdc", "verify"))


def test_source_rejects_app_login():
    with pytest.raises(ValueError, match="source"):
        _plan(side="source", purposes=("admin", "app", "cdc", "verify"))


def test_duplicate_login_names_are_rejected():
    logins = _logins("admin", "app", "sink", "verify")
    clash = (*logins[:3], dataclasses.replace(logins[3], name=logins[0].name))

    with pytest.raises(ValueError, match="겹친다"):
        _plan(logins=clash)


def test_table_names_must_be_schema_qualified():
    with pytest.raises(ValueError, match="orders"):
        _plan(tables=("orders",))


def test_tables_must_not_be_empty():
    with pytest.raises(ValueError, match="tables"):
        _plan(tables=())


def test_profile_secret_only_on_target():
    with pytest.raises(ValueError, match="대상"):
        _plan(
            side="source",
            purposes=("admin", "cdc", "verify"),
            app_profile_secret_id="profile",
        )


def _idle_connection():
    idle = psycopg.pq.TransactionStatus.IDLE
    return types.SimpleNamespace(
        info=types.SimpleNamespace(transaction_status=idle)
    )


def test_target_prepare_requires_schema_source():
    with pytest.raises(ValueError, match="schema_source"):
        datapath.prepare_db_principals(
            _plan(), _idle_connection(), MemorySecretStore()
        )


def test_prepare_refuses_open_transaction_before_writing_secrets():
    busy = types.SimpleNamespace(
        info=types.SimpleNamespace(
            transaction_status=psycopg.pq.TransactionStatus.INTRANS
        )
    )
    secrets = MemorySecretStore()

    with pytest.raises(ValueError, match="트랜잭션"):
        datapath.prepare_db_principals(
            _plan(side="source", purposes=("admin", "cdc", "verify")),
            busy,
            secrets,
        )

    assert secrets.versions == {}
