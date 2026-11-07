import os
from dataclasses import replace

import psycopg
import pytest
from psycopg import sql

from migration.config import UserSpec
from migration.source_cleanup import cleanup_source, drop_owned_publication


def test_connectors_block_all_db_changes(config, monkeypatch):
    monkeypatch.setattr('migration.source_cleanup.ConnectClient.exists', lambda *a: True)
    monkeypatch.setattr('migration.source_cleanup.db.connect', lambda *a: pytest.fail('DB must not be opened'))
    assert cleanup_source(config)['error'] == 'CONNECTORS_STILL_PRESENT'


def test_connector_error_is_redacted(config, monkeypatch):
    def broken(*a): raise RuntimeError('password=never-emit')
    monkeypatch.setattr('migration.source_cleanup.ConnectClient.exists', broken)
    result = cleanup_source(config)
    assert result['error'] == 'CONNECTORS_CHECK_FAILED'
    assert 'never-emit' not in str(result)


def test_wrong_target_is_refused(config):
    with pytest.raises(ValueError, match='SOURCE_TARGET_MISMATCH'):
        cleanup_source(config, config.job.target)


@pytest.mark.skipif(not os.environ.get('MIGRATION_CLEANUP_TEST_DSN'), reason='isolated PG DSN not configured')
def test_real_pg_separate_admin_and_publication_owner(config, env, monkeypatch):
    dsn = os.environ['MIGRATION_CLEANUP_TEST_DSN']
    with psycopg.connect(dsn, autocommit=True) as root:
        for role in ('cleanup_admin', 'cleanup_cdc'):
            root.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER REPLICATION').format(sql.Identifier(role)))
        root.execute('GRANT CREATE ON DATABASE postgres TO cleanup_cdc')
        root.execute('GRANT CREATE ON SCHEMA public TO cleanup_cdc')
        root.execute('SET ROLE cleanup_cdc')
        root.execute('CREATE TABLE cleanup_keep (id integer)')
        root.execute('INSERT INTO cleanup_keep VALUES (1)')
        root.execute('CREATE PUBLICATION forward_001_pub FOR TABLE cleanup_keep')
        root.execute('CREATE PUBLICATION unrelated_pub FOR TABLE cleanup_keep')
        root.execute('RESET ROLE')
        root.execute("SELECT pg_create_logical_replication_slot('forward_001','pgoutput')")
        # Reproduce the old code using a non-superuser admin as on RDS.
        root.execute('SET ROLE cleanup_admin')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            root.execute('DROP PUBLICATION forward_001_pub')
        root.execute('RESET ROLE')
    connection = psycopg.conninfo.conninfo_to_dict(dsn)
    targets = dict(config.targets)
    source = targets[config.job.source]
    targets[config.job.source] = replace(source, host=connection['host'], port=int(connection['port']),
        dbname='postgres', sslmode='disable', users={**source.users,
            'admin': UserSpec('cleanup_admin', 'MIGRATION_ADMIN_PASSWORD'),
            'cdc': UserSpec('cleanup_cdc', 'MIGRATION_CDC_PASSWORD')})
    config = replace(config, targets=targets)
    monkeypatch.setattr('migration.source_cleanup.ConnectClient.exists', lambda *a: False)
    first = cleanup_source(config)
    assert first['slot_status'] == 'dropped'
    assert first['publication_status'] == 'dropped'
    assert 'error' not in first
    second = cleanup_source(config)
    assert second['slot_status'] == second['publication_status'] == 'absent'
    with psycopg.connect(dsn, autocommit=True) as root:
        assert root.execute('SELECT count(*) FROM cleanup_keep').fetchone()[0] == 1
        assert root.execute("SELECT count(*) FROM pg_publication WHERE pubname='unrelated_pub'").fetchone()[0] == 1
        root.execute('SET ROLE cleanup_admin')
        with pytest.raises(ValueError, match='PUBLICATION_OWNER_MISMATCH'):
            drop_owned_publication(root, 'unrelated_pub')
        root.execute('RESET ROLE')
        root.execute('DROP PUBLICATION unrelated_pub')
        root.execute('DROP TABLE cleanup_keep')
        root.execute('REVOKE CREATE ON DATABASE postgres FROM cleanup_cdc')
        root.execute('REVOKE CREATE ON SCHEMA public FROM cleanup_cdc')
        for role in ('cleanup_admin', 'cleanup_cdc'):
            root.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
