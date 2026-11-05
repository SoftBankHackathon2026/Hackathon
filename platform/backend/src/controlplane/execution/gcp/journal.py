from enum import StrEnum

from psycopg.types.json import Jsonb


class JournalTable(StrEnum):
    DATA = "gcp_data"
    CACHE = "gcp_cache"


def save_checkpoint(conn, table: JournalTable, service_name: str, checkpoint: dict) -> None:
    conn.execute(
        f"UPDATE {table} SET checkpoint=%s,updated_at=now() WHERE service_name=%s",
        (Jsonb(checkpoint), service_name),
    )
    conn.commit()


def read_row(conn, table: JournalTable, service_name: str):
    return conn.execute(f"SELECT * FROM {table} WHERE service_name=%s", (service_name,)).fetchone()


def read_checkpoint(store, table: JournalTable, service_name: str) -> dict | None:
    with store.connect() as conn:
        row = conn.execute(
            f"SELECT checkpoint FROM {table} WHERE service_name=%s", (service_name,)
        ).fetchone()
    return row["checkpoint"] if row else None
