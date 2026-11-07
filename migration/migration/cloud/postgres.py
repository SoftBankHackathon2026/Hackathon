"""Persistent table fencing and bounded-memory, full-row verification."""

import json
import sqlite3
import tempfile
from contextlib import ExitStack
from decimal import Decimal

from psycopg import sql

from .. import db
from ..config import ConfigError


class Postgres:
    def __init__(self, plan):
        self.plan, self.config = plan, plan.config
        self.namespace = "_mg_" + self.config.job.job_id

    def connect(self, target, role="admin"):
        return db.connect(
            self.config.connection(target, role), statement_timeout_ms=600_000
        )

    def schema(self, conn, table):
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.relkind, c.relrowsecurity, c.relowner,
                                  EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=c.oid OR inhparent=c.oid)
                           FROM pg_class c WHERE c.oid=to_regclass(%s)""",
                (table.name,),
            )
            row = cur.fetchone()
            if not row or row[0] != "r" or row[1] or row[3]:
                raise ConfigError(
                    f"{table.name}: ordinary, non-RLS, non-inherited tables required"
                )
            owner = row[2]
            cur.execute(
                """SELECT a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull,
                                  a.attgenerated, a.attidentity, t.typname
                           FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
                           WHERE a.attrelid=to_regclass(%s) AND a.attnum>0 AND NOT a.attisdropped
                           ORDER BY a.attnum""",
                (table.name,),
            )
            columns = cur.fetchall()
            supported = {
                "int2",
                "int4",
                "int8",
                "text",
                "varchar",
                "bpchar",
                "bool",
                "numeric",
                "float4",
                "float8",
                "date",
                "timestamp",
                "timestamptz",
                "time",
                "uuid",
                "json",
                "jsonb",
                "bytea",
            }
            if any(r[3] or r[4] == "a" or r[5] not in supported for r in columns):
                raise ConfigError(
                    f"{table.name}: unsupported type/generated/always-identity column"
                )
            cur.execute(
                """SELECT a.attname FROM pg_index i
                           CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,n)
                           JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum
                           WHERE i.indrelid=to_regclass(%s) AND i.indisprimary ORDER BY k.n""",
                (table.name,),
            )
            if tuple(r[0] for r in cur.fetchall()) != table.primary_key:
                raise ConfigError(
                    f"{table.name}: declared primary key differs from database"
                )
            return {"columns": columns, "owner": owner}

    def precheck(self):
        c = self.config
        with self.connect(c.job.source) as src, self.connect(c.job.target) as tgt:
            ids = [db.server_identity(x).as_dict() for x in (src, tgt)]
            if all(
                ids[0][k] == ids[1][k]
                for k in ("database", "server_addr", "server_port")
            ):
                raise ConfigError("source and target resolve to the same database")
            with src.cursor() as cur:
                cur.execute("SHOW wal_level")
                if cur.fetchone()[0] != "logical":
                    raise ConfigError("source wal_level must be logical")
                cur.execute(
                    "SELECT 1 FROM pg_replication_slots WHERE slot_name=%s",
                    (c.job.slot_name,),
                )
                if cur.fetchone():
                    raise ConfigError(
                        "new job requires an unused replication slot name"
                    )
            for table in self.plan.tables:
                a, b = self.schema(src, table), self.schema(tgt, table)
                if a["columns"] != b["columns"]:
                    raise ConfigError(
                        f"{table.name}: source/target columns differ; provision schema first"
                    )
                for conn, name, metadata in (
                    (src, c.job.source, a),
                    (tgt, c.job.target, b),
                ):
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT 1 FROM pg_trigger WHERE tgrelid=to_regclass(%s)
                                       AND left(tgname,4)='_mg_' AND tgname<>ALL(%s)""",
                            (
                                table.name,
                                [self.namespace + "_stmt", self.namespace + "_row"],
                            ),
                        )
                        if cur.fetchone():
                            raise ConfigError(
                                "table is fenced by another migration; reconcile first"
                            )
                        app = c.target(name).user_spec("app").user
                        cur.execute(
                            "SELECT rolsuper OR rolbypassrls OR pg_has_role(oid,%s,'MEMBER') FROM pg_roles WHERE rolname=%s",
                            (metadata["owner"], app),
                        )
                        role = cur.fetchone()
                        if not role or role[0]:
                            raise ConfigError(
                                "app login must not own tables or bypass fencing"
                            )
                with tgt.cursor() as cur:
                    # Independent sinks do not preserve cross-table FK ordering, and user
                    # triggers can introduce side effects. Fail instead of silently disabling.
                    cur.execute(
                        """SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid=to_regclass(%s) AND contype='f')
                                   OR EXISTS(SELECT 1 FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
                                   JOIN pg_namespace n ON n.oid=p.pronamespace
                                   WHERE t.tgrelid=to_regclass(%s) AND NOT t.tgisinternal AND n.nspname<>%s)""",
                        (table.name, table.name, self.namespace),
                    )
                    if cur.fetchone()[0]:
                        raise ConfigError(
                            f"{table.name}: target foreign keys/user triggers require a separate schema rollout"
                        )
            return {"tables": len(self.plan.tables)}

    def fence(self, target, *, initial=False):
        """Atomic table locks drain existing writers before ALWAYS triggers commit.

        Source allows no writer. Destination permits only the dedicated sink login.
        Statement + row triggers cover zero-row writes, TRUNCATE and FK cascades.
        The namespace marker detects reuse by a different manifest.
        """
        allowed = (
            self.config.target(target).user_spec("sink").user
            if target == self.config.job.target
            else ""
        )
        ns = sql.Identifier(self.namespace)
        with self.connect(target) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute(
                sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                    sql.SQL(",").join(db.table_ident(t.name) for t in self.plan.tables)
                )
            )
            cur.execute(
                "SELECT obj_description(oid,'pg_namespace') FROM pg_namespace WHERE nspname=%s",
                (self.namespace,),
            )
            row = cur.fetchone()
            if row and row[0] != self.plan.fingerprint:
                raise ConfigError("fence namespace already belongs to another plan")
            if initial and not row:
                for table in self.plan.tables:
                    if db.count_rows(conn, table.name):
                        raise ConfigError(
                            f"{table.name}: new target must be empty; never overwrite existing data"
                        )
            if not row:
                cur.execute(sql.SQL("CREATE SCHEMA {}").format(ns))
                cur.execute(
                    sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                        ns, sql.Literal(self.plan.fingerprint)
                    )
                )
            # Function body has no interpolated identifiers or credentials.
            body = """BEGIN
                IF TG_NARGS = 1 AND session_user = TG_ARGV[0] THEN
                    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'migration write fence' USING ERRCODE = '42501';
            END"""
            cur.execute(
                sql.SQL(
                    "CREATE OR REPLACE FUNCTION {}.fence() RETURNS trigger LANGUAGE plpgsql AS {}"
                ).format(ns, sql.Literal(body))
            )
            for table in self.plan.tables:
                for suffix, events, level in (
                    ("stmt", "INSERT OR UPDATE OR DELETE OR TRUNCATE", "STATEMENT"),
                    ("row", "INSERT OR UPDATE OR DELETE", "ROW"),
                ):
                    trigger = sql.Identifier(self.namespace + "_" + suffix)
                    cur.execute(
                        sql.SQL(
                            "CREATE OR REPLACE TRIGGER {} BEFORE {} ON {} FOR EACH {} EXECUTE FUNCTION {}.fence({})"
                        ).format(
                            trigger,
                            sql.SQL(events),
                            db.table_ident(table.name),
                            sql.SQL(level),
                            ns,
                            sql.Literal(allowed),
                        )
                    )
                    cur.execute(
                        sql.SQL("ALTER TABLE {} ENABLE ALWAYS TRIGGER {}").format(
                            db.table_ident(table.name), trigger
                        )
                    )
            conn.commit()

    def assert_fenced(self, target):
        with self.connect(target) as conn, conn.cursor() as cur:
            for table in self.plan.tables:
                cur.execute(
                    """SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
                               JOIN pg_namespace n ON n.oid=p.pronamespace WHERE t.tgrelid=to_regclass(%s)
                               AND n.nspname=%s AND t.tgenabled='A' AND t.tgname=ANY(%s)""",
                    (
                        table.name,
                        self.namespace,
                        [self.namespace + "_stmt", self.namespace + "_row"],
                    ),
                )
                if cur.fetchone()[0] != 2:
                    raise ConfigError(
                        f"{table.name}: persistent fence missing or disabled"
                    )

    def release_target(self):
        with self.connect(self.config.job.target) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '10s'")
            for table in self.plan.tables:
                for suffix in ("stmt", "row"):
                    cur.execute(
                        sql.SQL("DROP TRIGGER IF EXISTS {} ON {}").format(
                            sql.Identifier(self.namespace + "_" + suffix),
                            db.table_ident(table.name),
                        )
                    )
            conn.commit()

    def prepare(self):
        c = self.config
        with self.connect(c.job.source) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_publication WHERE pubname=%s",
                (c.job.publication_name,),
            )
            if not cur.fetchone():
                cur.execute(
                    sql.SQL(
                        "CREATE PUBLICATION {} FOR TABLE {} WITH (publish = {})"
                    ).format(
                        sql.Identifier(c.job.publication_name),
                        sql.SQL(",").join(
                            db.table_ident(t.name) for t in self.plan.tables
                        ),
                        sql.Literal("insert, update, delete"),
                    )
                )
                cur.execute(
                    sql.SQL("ALTER PUBLICATION {} OWNER TO {}").format(
                        sql.Identifier(c.job.publication_name),
                        sql.Identifier(c.target(c.job.source).user_spec("cdc").user),
                    )
                )
            cur.execute(
                "SELECT schemaname,tablename FROM pg_publication_tables WHERE pubname=%s",
                (c.job.publication_name,),
            )
            if {".".join(r) for r in cur.fetchall()} != {
                t.name for t in self.plan.tables
            }:
                raise ConfigError("existing publication scope mismatch")
            conn.commit()
        # Debezium creates its own slot, so snapshot and starting LSN stay coupled.

    def sync_sequences(self):
        """Transfer owned positive noncycling serial sequences while both apps are fenced."""
        c = self.config
        with self.connect(c.job.source) as src, self.connect(c.job.target) as tgt:
            for table in self.plan.tables:
                with src.cursor() as sc, tgt.cursor() as tc:
                    sc.execute(
                        "SELECT attname FROM pg_attribute WHERE attrelid=to_regclass(%s) AND attnum>0 AND NOT attisdropped",
                        (table.name,),
                    )
                    for (col,) in sc.fetchall():
                        sc.execute(
                            "SELECT pg_get_serial_sequence(%s,%s)", (table.name, col)
                        )
                        seq = sc.fetchone()[0]
                        tc.execute(
                            "SELECT pg_get_serial_sequence(%s,%s)", (table.name, col)
                        )
                        dest_seq = tc.fetchone()[0]
                        if bool(seq) != bool(dest_seq):
                            raise ConfigError("source/target owned sequence mismatch")
                        if not seq:
                            continue
                        sc.execute(
                            "SELECT seqincrement,seqcycle FROM pg_sequence WHERE seqrelid=%s::regclass",
                            (seq,),
                        )
                        if sc.fetchone() != (1, False):
                            raise ConfigError(
                                "only increment=1, noncycling owned sequences supported"
                            )
                        sc.execute(
                            sql.SQL("SELECT last_value,is_called FROM {}").format(
                                db.table_ident(seq)
                            )
                        )
                        last, called = sc.fetchone()
                        tc.execute(
                            sql.SQL("SELECT max({}) FROM {}").format(
                                sql.Identifier(col), db.table_ident(table.name)
                            )
                        )
                        maximum = tc.fetchone()[0]
                        if maximum is not None and maximum >= last:
                            last, called = maximum, True
                        tc.execute(
                            "SELECT setval(%s::regclass,%s,%s)",
                            (dest_seq, last, called),
                        )
            tgt.commit()

    def verify(self):
        """Every column and PK compared exactly, spooled on disk; report counts only.

        No customer row values/keys enter logs or reports. Decimal JSON decoding
        avoids float rounding. PostgreSQL renders bytea/date/time in a fixed session.
        """
        reports = {}
        c = self.config
        with (
            tempfile.TemporaryDirectory(prefix="migration-verify-") as directory,
            ExitStack() as stack,
        ):
            spool = sqlite3.connect(directory + "/rows.sqlite")
            try:
                spool.execute(
                    "CREATE TABLE rows(side TEXT, tab TEXT, key TEXT, value TEXT, PRIMARY KEY(side,tab,key))"
                )
                for side, target in (("s", c.job.source), ("t", c.job.target)):
                    conn = stack.enter_context(self.connect(target, "verify"))
                    conn.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    conn.execute("SET LOCAL timezone='UTC'")
                    conn.execute("SET LOCAL datestyle='ISO, YMD'")
                    conn.execute("SET LOCAL bytea_output='hex'")
                    conn.execute("SET LOCAL extra_float_digits=3")
                    conn.execute("SET LOCAL row_security=off")
                    for table in self.plan.tables:
                        self.schema(conn, table)
                        with conn.cursor(name="cloud_verify") as cur:
                            cur.itersize = 2000
                            cur.execute(
                                sql.SQL("SELECT to_jsonb(t)::text FROM {} t").format(
                                    db.table_ident(table.name)
                                )
                            )
                            for (text,) in cur:
                                value = json.loads(
                                    text, parse_float=Decimal, parse_int=Decimal
                                )
                                key = [value[k] for k in table.primary_key]
                                spool.execute(
                                    "INSERT INTO rows VALUES (?,?,?,?)",
                                    (
                                        side,
                                        table.name,
                                        canonical(key),
                                        canonical(value),
                                    ),
                                )
                    conn.commit()
                for table in self.plan.tables:
                    missing = spool.execute(
                        "SELECT count(*) FROM rows s LEFT JOIN rows t ON t.side='t' AND t.tab=s.tab AND t.key=s.key WHERE s.side='s' AND s.tab=? AND t.key IS NULL",
                        (table.name,),
                    ).fetchone()[0]
                    extra = spool.execute(
                        "SELECT count(*) FROM rows t LEFT JOIN rows s ON s.side='s' AND s.tab=t.tab AND s.key=t.key WHERE t.side='t' AND t.tab=? AND s.key IS NULL",
                        (table.name,),
                    ).fetchone()[0]
                    changed = spool.execute(
                        "SELECT count(*) FROM rows s JOIN rows t ON t.side='t' AND t.tab=s.tab AND t.key=s.key WHERE s.side='s' AND s.tab=? AND s.value<>t.value",
                        (table.name,),
                    ).fetchone()[0]
                    counts = {
                        side: spool.execute(
                            "SELECT count(*) FROM rows WHERE side=? AND tab=?",
                            (side, table.name),
                        ).fetchone()[0]
                        for side in ("s", "t")
                    }
                    reports[table.name] = {
                        "missing": missing,
                        "extra": extra,
                        "changed": changed,
                        "source": counts["s"],
                        "target": counts["t"],
                    }
                return {
                    "passed": all(
                        not any(r[k] for k in ("missing", "extra", "changed"))
                        for r in reports.values()
                    ),
                    "tables": reports,
                }
            finally:
                spool.close()


def canonical(value):
    # Type tags distinguish a string from a number without precision loss.
    def normalize(v):
        if isinstance(v, Decimal):
            # normalize() is context-sensitive and may round large decimals.
            text = format(v, "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            if v == 0:
                text = "0"
            return ["number", text]
        if isinstance(v, dict):
            return ["object", [[k, normalize(v[k])] for k in sorted(v)]]
        if isinstance(v, list):
            return ["array", [normalize(x) for x in v]]
        return [type(v).__name__, v]

    return json.dumps(normalize(value), ensure_ascii=False, separators=(",", ":"))
