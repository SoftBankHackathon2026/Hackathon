from enum import IntEnum

from psycopg import Connection


class AdvisoryLock(IntEnum):
    MIGRATION = 1
    RELEASE_INTAKE = 2
    WORKER = 3
    SOURCE_BUILDS = 4
    CATALOG = 5
    CONFIGURATION = 6
    DEMO_RUNS = 7


def xact_lock(conn: Connection, lock: AdvisoryLock) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), %s)", (int(lock),))


def try_shared_xact_lock(conn: Connection, lock: AdvisoryLock) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock_shared(hashtext(current_schema()), %s) AS locked",
        (int(lock),),
    ).fetchone()
    return row["locked"]


def session_lock(conn: Connection, lock: AdvisoryLock) -> None:
    conn.execute("SELECT pg_advisory_lock(hashtext(current_schema()), %s)", (int(lock),))


def try_session_lock(conn: Connection, lock: AdvisoryLock) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(current_schema()), %s) AS locked", (int(lock),)
    ).fetchone()
    return row["locked"]


def session_unlock(conn: Connection, lock: AdvisoryLock) -> None:
    conn.execute("SELECT pg_advisory_unlock(hashtext(current_schema()), %s)", (int(lock),))
