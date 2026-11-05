"""Each journal test owns an isolated real PostgreSQL schema."""

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from controlplane.repositories.releases import ReleaseStore

DBFILE = Path(__file__).resolve().parents[2] / ".data/database.json"


@pytest.fixture
def store():
    dsn = os.getenv("ONEPORT_TEST_DATABASE_URL")
    if not dsn and not DBFILE.exists():
        pytest.skip("Dedicated PostgreSQL not configured: scripts/local_database.py")
    dsn = dsn or json.loads(DBFILE.read_text())["database_url"]
    schema = "lp_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    instance = ReleaseStore(make_conninfo(dsn, options=f"-csearch_path={schema}"))
    instance.migrate()
    yield instance
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
