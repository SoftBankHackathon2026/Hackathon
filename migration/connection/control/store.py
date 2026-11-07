"""Aggregate-level PostgreSQL row locks serialize proof, approval and registration."""

import threading
from contextlib import contextmanager
from copy import deepcopy

from migration_connection.control.errors import ConnectionError
from psycopg.types.json import Jsonb


class PostgresStore:
    def __init__(self, connect):
        self.connect = connect

    def create(self, row):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO connection_requests(id,team,owner,operation_id,document) "
                "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(team,owner,operation_id) DO NOTHING",
                (row["id"], row["team"], row["owner"], row["operation_id"], Jsonb(row)),
            )
            return conn.execute(
                "SELECT document FROM connection_requests WHERE team=%s AND owner=%s AND operation_id=%s",
                (row["team"], row["owner"], row["operation_id"]),
            ).fetchone()["document"]

    def get(self, request_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT document FROM connection_requests WHERE id=%s", (request_id,)
            ).fetchone()
            return row["document"] if row else None

    def list(self, team):
        with self.connect() as conn:
            return [
                row["document"]
                for row in conn.execute(
                    "SELECT document FROM connection_requests WHERE team=%s ORDER BY created_at DESC LIMIT 100",
                    (team,),
                ).fetchall()
            ]

    @contextmanager
    def locked(self, request_id):
        error = None
        with self.connect() as conn:
            value = conn.execute(
                "SELECT document FROM connection_requests WHERE id=%s FOR UPDATE",
                (request_id,),
            ).fetchone()
            if value is None:
                raise ConnectionError("CONNECTION_NOT_FOUND", 404)
            row = value["document"]
            try:
                yield row
            except ConnectionError as caught:
                # Expected rejections may consume a nonce or count an approval failure.
                error = caught
            conn.execute(
                "UPDATE connection_requests SET document=%s WHERE id=%s",
                (Jsonb(row), request_id),
            )
        if error:
            raise error


class MemoryStore:
    """Test double only. Production always constructs PostgresStore."""

    def __init__(self):
        self.rows = {}
        self.mutex = threading.RLock()

    def create(self, row):
        with self.mutex:
            existing = next(
                (
                    r
                    for r in self.rows.values()
                    if (r["team"], r["owner"], r["operation_id"])
                    == (row["team"], row["owner"], row["operation_id"])
                ),
                None,
            )
            if existing is None:
                self.rows[row["id"]] = deepcopy(row)
            return deepcopy(existing or row)

    def get(self, request_id):
        with self.mutex:
            return deepcopy(self.rows.get(request_id))

    def list(self, team):
        with self.mutex:
            return deepcopy([row for row in self.rows.values() if row["team"] == team])

    @contextmanager
    def locked(self, request_id):
        with self.mutex:
            if request_id not in self.rows:
                raise ConnectionError("CONNECTION_NOT_FOUND", 404)
            row = deepcopy(self.rows[request_id])
            try:
                yield row
            except ConnectionError:
                self.rows[request_id] = row
                raise
            else:
                self.rows[request_id] = row
