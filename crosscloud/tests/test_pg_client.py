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


def _versions(dump: str, psql: str) -> pg_client.PgTools:
    # $0 이 도구 이름이다. 버전 문자열은 실제 출력 형식을 따른다.
    return _shell(
        f'case "$0" in pg_dump) echo "pg_dump (PostgreSQL) {dump}";; '
        f'psql) echo "psql (PostgreSQL) {psql}";; esac'
    )


def test_major_version_reads_tool_output():
    tools = _versions("16.15 (Debian 16.15-1.pgdg13+2)", "17.10")

    assert tools.major_version("pg_dump") == 16
    assert tools.major_version("psql") == 17


def test_matching_versions_pass():
    _versions("16.15", "16.15").require_versions(
        source_major=16, target_major=16
    )


@pytest.mark.parametrize(
    ("dump", "psql", "source", "target"),
    [
        ("17.10", "17.10", 16, 16),  # 새 pg_dump 덤프는 옛 서버에서 실패한다.
        ("16.15", "16.15", 17, 17),  # 옛 pg_dump 는 새 서버를 못 뜬다.
        ("16.15", "17.10", 16, 16),  # psql 은 대상과 같아야 한다.
        ("17.10", "16.15", 17, 16),  # 새 서버 덤프를 옛 서버로 내리지 않는다.
    ],
)
def test_mismatched_versions_are_refused(dump, psql, source, target):
    with pytest.raises(pg_client.PgToolError, match="주 버전"):
        _versions(dump, psql).require_versions(
            source_major=source, target_major=target
        )


def test_unparseable_version_output_is_refused():
    with pytest.raises(pg_client.PgToolError, match="버전"):
        _shell('echo "nonsense"').major_version("pg_dump")
