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

    def list(self, team, *, include_observations=False):
        query = (
            "SELECT document, observation FROM connection_requests"
            if include_observations
            else "SELECT document FROM connection_requests"
        )
        with self.connect() as conn:
            rows = conn.execute(
                query + " WHERE team=%s ORDER BY created_at DESC LIMIT 100", (team,)
            ).fetchall()
        return [
            {**row["document"], "observation": row["observation"]}
            if include_observations
            else row["document"]
            for row in rows
        ]

    @contextmanager
    def locked(self, request_id):
        with self.locked_rows(request_id) as rows:
            yield rows[request_id]

    @contextmanager
    def locked_rows(self, *request_ids):
        error = None
        with self.connect() as conn:
            rows = {}
            for request_id in sorted(set(request_ids)):
                value = conn.execute(
                    "SELECT document FROM connection_requests WHERE id=%s FOR UPDATE",
                    (request_id,),
                ).fetchone()
                if value is None:
                    raise ConnectionError("CONNECTION_NOT_FOUND", 404)
                rows[request_id] = value["document"]
            try:
                yield rows
            except ConnectionError as caught:
                # Expected rejections may consume a nonce or count an approval failure.
                error = caught
            for request_id, row in rows.items():
                if "observation" in row:
                    latest = row.pop("observation")
                    conn.execute(
                        "UPDATE connection_requests SET document=%s, observation=%s WHERE id=%s",
                        (Jsonb(row), Jsonb(latest), request_id),
                    )
                else:
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
        self.observations = {}
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

    def list(self, team, *, include_observations=False):
        with self.mutex:
            return deepcopy(
                [
                    {**row, "observation": self.observations.get(row["id"])}
                    if include_observations
                    else row
                    for row in self.rows.values()
                    if row["team"] == team
                ]
            )

    def _save(self, rows):
        for request_id, row in rows.items():
            if "observation" in row:
                self.observations[request_id] = row.pop("observation")
        self.rows.update(rows)

    @contextmanager
    def locked(self, request_id):
        with self.locked_rows(request_id) as rows:
            yield rows[request_id]

    @contextmanager
    def locked_rows(self, *request_ids):
        with self.mutex:
            if any(request_id not in self.rows for request_id in request_ids):
                raise ConnectionError("CONNECTION_NOT_FOUND", 404)
            rows = {
                request_id: deepcopy(self.rows[request_id])
                for request_id in request_ids
            }
            try:
                yield rows
            except ConnectionError:
                self._save(rows)
                raise
            else:
                self._save(rows)
