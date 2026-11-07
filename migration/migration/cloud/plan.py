"""Explicit application scope; never infer demo columns or cloud provider names."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from ..config import Config, ConfigError, parse_config

IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class Table:
    name: str
    primary_key: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    config: Config
    tables: tuple[Table, ...]
    application_url: str

    @property
    def fingerprint(self):
        # Contains environment-variable *names*, never password values.
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True).encode()
        ).hexdigest()


def parse(raw):
    config = parse_config(raw)
    app = raw.get("application", {})
    if set(app) - {"tables", "control_url"}:
        raise ConfigError("application: unknown keys")
    tables = []
    for entry in app.get("tables", []):
        if not isinstance(entry, dict) or set(entry) != {"name", "primary_key"}:
            raise ConfigError("application.tables needs name and primary_key")
        name, pk = entry["name"], entry["primary_key"]
        if (
            not isinstance(name, str)
            or len(name.split(".")) != 2
            or not all(IDENT.fullmatch(x) for x in name.split("."))
        ):
            raise ConfigError("table must be schema.table using lower-case identifiers")
        if (
            not isinstance(pk, list)
            or not pk
            or not all(isinstance(x, str) and IDENT.fullmatch(x) for x in pk)
            or len(pk) != len(set(pk))
        ):
            raise ConfigError(
                "primary_key must be a nonempty list of distinct column names"
            )
        tables.append(Table(name, tuple(pk)))
    if not tables or len({t.name for t in tables}) != len(tables):
        raise ConfigError("application.tables must be nonempty and unique")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", config.job.job_id):
        raise ConfigError("cloud job_id must be lower-case and at most 40 characters")
    url = app.get("control_url", "")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ConfigError("application.control_url is required")
    src, tgt = config.target(config.job.source), config.target(config.job.target)
    if (src.host, src.port, src.dbname) == (tgt.host, tgt.port, tgt.dbname):
        raise ConfigError("source and target identify the same database")
    for dest in (src, tgt):
        if dest.user_spec("app").user in {
            dest.user_spec("admin").user,
            dest.user_spec("cdc").user,
            dest.user_spec("sink").user,
        }:
            raise ConfigError("application login must be separate from migration roles")
    return Plan(config, tuple(sorted(tables, key=lambda t: t.name)), url.rstrip("/"))


def load(path: str | Path):
    with Path(path).open() as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a mapping")
    return parse(raw)


def connectors(plan):
    """One native-envelope JDBC sink per table, including composite PK and deletes."""
    c = plan.config
    src, tgt = c.target(c.job.source), c.target(c.job.target)
    common = {
        "tasks.max": "1",
        "errors.tolerance": "none",
        "key.converter": "org.apache.kafka.connect.json.JsonConverter",
        "key.converter.schemas.enable": "true",
        "value.converter": "org.apache.kafka.connect.json.JsonConverter",
        "value.converter.schemas.enable": "true",
    }
    source = {
        **common,
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "plugin.name": "pgoutput",
        "snapshot.mode": "initial",
        "database.hostname": src.host,
        "database.port": str(src.port),
        "database.dbname": src.dbname,
        "database.sslmode": src.sslmode,
        "database.user": src.user_spec("cdc").user,
        "database.password": "${env:" + src.user_spec("cdc").password_env + "}",
        "topic.prefix": c.job.job_id,
        "slot.name": c.job.slot_name,
        "slot.drop.on.stop": "false",
        "publication.name": c.job.publication_name,
        "publication.autocreate.mode": "disabled",
        "table.include.list": ",".join(re.escape(t.name) for t in plan.tables),
        "tombstones.on.delete": "true",
        "decimal.handling.mode": "precise",
        "time.precision.mode": "adaptive_time_microseconds",
        "heartbeat.interval.ms": "1000",
    }
    docs = {c.job.source_connector: source}
    for i, table in enumerate(plan.tables):
        docs[f"{c.job.job_id}-sink-{i}"] = {
            **common,
            "connector.class": "io.debezium.connector.jdbc.JdbcSinkConnector",
            "topics": f"{c.job.job_id}.{table.name}",
            "connection.url": f"jdbc:postgresql://{tgt.host}:{tgt.port}/{tgt.dbname}?sslmode={tgt.sslmode}",
            "connection.username": tgt.user_spec("sink").user,
            "connection.password": "${env:" + tgt.user_spec("sink").password_env + "}",
            "insert.mode": "upsert",
            "primary.key.mode": "record_key",
            "primary.key.fields": ",".join(table.primary_key),
            "delete.enabled": "true",
            "schema.evolution": "none",
            "quote.identifiers": "true",
            "table.name.format": table.name,
        }
    return docs


def snapshot_complete(doc, job_id):
    """Debezium PG 3.x offsets: explicit completed snapshot or streaming LSN.

    Missing/unavailable offsets and an in-progress snapshot never pass, even for
    empty tables. Counts are deliberately not used as snapshot evidence.
    """
    entries = [
        e.get("offset", {})
        for e in (doc or {}).get("offsets", [])
        if e.get("partition", {}).get("server") == job_id
    ]
    if len(entries) != 1:
        return False
    off = entries[0]
    if off.get("snapshot_completed") is True or off.get("last_snapshot_record") is True:
        return True
    return (
        "snapshot" not in off or off["snapshot"] in (False, "false")
    ) and isinstance(off.get("lsn"), int)
