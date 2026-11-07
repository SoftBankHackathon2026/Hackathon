import dataclasses
import logging

import pytest
from memory_secret_store import MemorySecretStore

from crosscloud import datapath, pg_client
from crosscloud.secret_transfer import store

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
        "source_app_user": "shop_app",
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


_SOURCE = ("admin", "cdc", "verify")
_BOOTSTRAP = pg_client.PgLogin(
    dataclasses.replace(_ENDPOINT, database="postgres"), "postgres", b"x"
)


def test_source_login_must_not_reuse_source_app_name():
    logins = (
        *_logins("admin", "cdc"),
        datapath.DbLogin(purpose="verify", name="shop_app", secret_id="s-v"),
    )

    with pytest.raises(ValueError, match="원본 앱"):
        _plan(side="source", purposes=_SOURCE, logins=logins)


def test_target_prepare_requires_schema_source():
    with pytest.raises(ValueError, match="schema_source"):
        datapath.prepare_db_principals(_plan(), _BOOTSTRAP, MemorySecretStore())


def test_bootstrap_must_point_at_the_plan_server():
    elsewhere = dataclasses.replace(
        _BOOTSTRAP,
        endpoint=dataclasses.replace(_BOOTSTRAP.endpoint, host="other.invalid"),
    )

    with pytest.raises(ValueError, match="bootstrap"):
        datapath.prepare_db_principals(
            _plan(side="source", purposes=_SOURCE),
            elsewhere,
            MemorySecretStore(),
        )


def test_prepare_refuses_sdk_debug_logging_before_touching_secrets():
    logger = logging.getLogger("botocore.parsers")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    secrets = MemorySecretStore()
    try:
        with pytest.raises(store.UnsafeLoggingError, match="botocore.parsers"):
            datapath.prepare_db_principals(
                _plan(side="source", purposes=_SOURCE),
                _BOOTSTRAP,
                secrets,
            )
    finally:
        logger.setLevel(previous)

    assert secrets.reads == []


@pytest.mark.parametrize(
    "row",
    [
        ("TABLE", "public", "orders", "SELECT; DROP TABLE x"),
        ("TABLE", "public", "orders", "TRUNCATE"),
        ("FUNCTION", "public", "f", "EXECUTE"),
        ("SEQUENCE", "public", "orders_id_seq", "INSERT"),
        ("SCHEMA", "shop", None, "CREATE"),
    ],
)
def test_mirrored_grant_rejects_unexpected_kind_or_privilege(row):
    with pytest.raises(datapath.PrincipalError, match="미러"):
        datapath.MirroredGrant.from_row(row)


def test_mirrored_grant_quotes_server_names_as_identifiers():
    grant = datapath.MirroredGrant.from_row(
        ("SEQUENCE", "public", 'odd"; DROP TABLE x; --', "USAGE")
    )

    statement = grant.statement("tapp").as_string(None)

    assert statement == (
        'GRANT USAGE ON SEQUENCE "public"."odd""; DROP TABLE x; --" TO "tapp"'
    )
