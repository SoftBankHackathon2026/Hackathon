"""Real PostgreSQL: reuse, transaction cleanup, and replacement after disconnect."""
import os

import psycopg
import pytest

from migration.config import ConnectionSpec
from migration.db import connection_pool
from tests.conftest import requires_pg


@requires_pg
def test_pool_reuses_rolls_back_and_replaces_broken_connection():
    params = psycopg.conninfo.conninfo_to_dict(os.environ['MIGRATION_TEST_DSN'])
    spec = ConnectionSpec(target='test', role='admin', host=params.get('host', '127.0.0.1'), port=int(params.get('port', 5432)),
                          dbname=params.get('dbname', 'postgres'), user=params.get('user', 'postgres'),
                          password=params.get('password', ''), sslmode='disable')
    pool = connection_pool(spec)
    try:
        pool.wait()
        with pool.connection() as conn:
            pid = conn.info.backend_pid
            assert conn.execute('SHOW statement_timeout').fetchone()[0] == '30s'
        for _ in range(13):
            with pool.connection() as conn:
                assert conn.info.backend_pid == pid
        with pytest.raises(psycopg.errors.DivisionByZero):
            with pool.connection() as conn:
                conn.execute('SELECT 1/0')
        with pool.connection() as conn:
            assert conn.info.backend_pid == pid
            assert conn.execute('SELECT 42').fetchone()[0] == 42
        with psycopg.connect(os.environ['MIGRATION_TEST_DSN'], autocommit=True) as admin:
            admin.execute('SELECT pg_terminate_backend(%s)', (pid,))
        with pool.connection() as conn:
            assert conn.info.backend_pid != pid
            assert conn.execute('SELECT 42').fetchone()[0] == 42
    finally:
        pool.close()
    assert pool.closed
