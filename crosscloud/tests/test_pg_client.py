import pytest

from crosscloud import pg_client

_CANARY = b"canary-pg-0c55-should-not-leak"
_ENDPOINT = pg_client.DbEndpoint(
    host="db.example.invalid", port=5432, database="shop", sslmode="require"
)
_LOGIN = pg_client.PgLogin(
    endpoint=_ENDPOINT, user="migration_admin", password=_CANARY
)


def _shell(script: str) -> pg_client.PgTools:
    # 접두사 뒤에 붙는 도구 인자는 sh 의 $0·$1… 이 된다.
    return pg_client.PgTools(command_prefix=("sh", "-c", script))


def test_login_repr_hides_password():
    assert _CANARY.decode() not in repr(_LOGIN)
    assert _CANARY.decode() not in str(_LOGIN)


def test_dump_passes_password_in_env_not_argv():
    tools = _shell('printf "%s|%s|%s" "$PGPASSWORD" "$PGSSLMODE" "$*"')

    out = tools.dump_schema(_LOGIN, ("public.orders", "shop.items"))

    password, sslmode, args = out.decode().split("|")
    assert password == _CANARY.decode()
    assert sslmode == "require"
    assert _CANARY.decode() not in args
    for expected in (
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--table=public.orders",
        "--table=shop.items",
        "--host=db.example.invalid",
        "--username=migration_admin",
        "--dbname=shop",
    ):
        assert expected in args


def test_run_script_feeds_stdin_with_stop_on_error():
    tools = _shell('cat >/dev/null; printf "%s" "$*" >&2; exit 0')

    tools.run_script(_LOGIN, b"SELECT 1;")


def test_tool_failure_raises_without_password():
    tools = _shell('cat >/dev/null; echo "ERROR: relation exists" >&2; exit 3')

    with pytest.raises(pg_client.PgToolError) as excinfo:
        tools.run_script(_LOGIN, b"CREATE TABLE t();")

    message = str(excinfo.value)
    assert "psql" in message
    assert "relation exists" in message
    assert _CANARY.decode() not in message


def test_dump_rejects_unqualified_table_name():
    with pytest.raises(ValueError, match="orders"):
        _shell("true").dump_schema(_LOGIN, ("orders",))
