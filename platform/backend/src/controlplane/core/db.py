"""Journal database connections.

One-shot commands and tests open a connection per use. Long-running processes
(the API and the workers) turn pooling on: opening a PostgreSQL connection costs a
password-hash handshake, and doing that for every query was most of their CPU.

A pooled connection is used exactly like a fresh one: `with connect(dsn) as conn`
commits on success, rolls back on error, and gives the connection back.
"""

import threading
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_lock = threading.Lock()
_pools: dict[str, ConnectionPool] = {}
_users = 0
_max_size = 4


def connect(dsn: str):
    if not _users:
        return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5)
    with _lock:
        pool = _pools.get(dsn)
        if pool is None:
            pool = _pools[dsn] = ConnectionPool(
                dsn,
                kwargs={"row_factory": dict_row, "connect_timeout": 5},
                min_size=1,
                max_size=_max_size,
                timeout=10,
                reset=_reset,
                check=ConnectionPool.check_connection,
                open=True,
            )
    return pool.connection()


def _reset(conn):
    # Callers rely on "closing the connection releases my session lock".
    conn.row_factory = dict_row
    conn.execute("SELECT pg_advisory_unlock_all()")
    conn.commit()


@contextmanager
def pooling(max_size: int = 4):
    """Pool connections while the block runs; the pools are closed after the last user."""
    global _users, _max_size
    with _lock:
        _users += 1
        _max_size = max(_max_size, max_size)
    try:
        yield
    finally:
        with _lock:
            _users -= 1
            closing = [] if _users else list(_pools.values())
            if not _users:
                _pools.clear()
        for pool in closing:
            pool.close()
