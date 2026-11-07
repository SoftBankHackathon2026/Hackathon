"""통합 시험용 PostgreSQL 준비물 — 실행 하나마다 역할·DB 를 만들고 지운다.

환경변수가 없으면 시험을 건너뛴다.

*   CROSSCLOUD_TEST_PG_DSN: 시험 전용 서버의 슈퍼유저 libpq DSN(운영 DB 금지).
*   CROSSCLOUD_TEST_PG_TOOLS: pg_dump·psql 앞에 붙일 명령(선택). 예:
    `docker exec -i -e PGPASSWORD -e PGSSLMODE -e PGCONNECT_TIMEOUT r4-pg16`.
*   CROSSCLOUD_TEST_PG_CONTAINER: 서버 로그를 읽을 컨테이너 이름(선택).

클라우드의 관리 사용자(RDS master·Cloud SQL postgres)는 슈퍼유저가 아니므로
CREATEROLE·CREATEDB 만 가진 master 역할을 만들어 그 이름으로 준비한다. RDS 의
rds_replication 역할도 흉내 낸다. 대상 DB 는 만들지 않는다(준비가 만든다).
"""

import dataclasses
import os
import secrets
import shlex

import psycopg
from psycopg import sql

from crosscloud import pg_client

DSN = os.environ.get("CROSSCLOUD_TEST_PG_DSN", "")
TOOLS = pg_client.PgTools(
    command_prefix=tuple(
        shlex.split(os.environ.get("CROSSCLOUD_TEST_PG_TOOLS", ""))
    )
)
CONTAINER = os.environ.get("CROSSCLOUD_TEST_PG_CONTAINER", "")

_SOURCE_SCHEMA = """
CREATE TABLE public.orders (
    id serial PRIMARY KEY,
    note text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE SCHEMA shop;
CREATE TABLE shop.items (
    item_id uuid PRIMARY KEY,
    payload jsonb NOT NULL
);
INSERT INTO public.orders (note) VALUES ('source row');
"""


@dataclasses.dataclass(frozen=True)
class Cluster:
    """실행 하나의 이름표와 접속 정보."""

    suffix: str
    master: str
    master_password: bytes
    source: pg_client.DbEndpoint
    target: pg_client.DbEndpoint
    source_app: str

    def name(self, base: str) -> str:
        return f"{base}_{self.suffix}"

    def master_login(self, endpoint: pg_client.DbEndpoint) -> pg_client.PgLogin:
        return pg_client.PgLogin(endpoint, self.master, self.master_password)

    def bootstrap(self) -> pg_client.PgLogin:
        """관리 DB(postgres)에 붙는 master 로그인."""
        maintenance = dataclasses.replace(self.target, database="postgres")
        return self.master_login(maintenance)

    def superuser(self, database: str) -> psycopg.Connection:
        return psycopg.connect(DSN, dbname=database, autocommit=True)


def _endpoint(database: str) -> pg_client.DbEndpoint:
    info = psycopg.conninfo.conninfo_to_dict(DSN)
    return pg_client.DbEndpoint(
        host=str(info.get("host", "127.0.0.1")),
        port=int(info.get("port", 5432)),
        database=database,
        sslmode=str(info.get("sslmode", "disable")),
    )


def create() -> Cluster:
    suffix = secrets.token_hex(3)
    cluster = Cluster(
        suffix=suffix,
        master=f"master_{suffix}",
        master_password=secrets.token_urlsafe(24).encode(),
        source=_endpoint(f"src_{suffix}"),
        target=_endpoint(f"tgt_{suffix}"),
        source_app=f"sapp_{suffix}",
    )
    with cluster.superuser("postgres") as conn:
        conn.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN CREATEROLE CREATEDB PASSWORD {}"
            ).format(
                sql.Identifier(cluster.master),
                sql.Literal(cluster.master_password.decode()),
            )
        )
        conn.execute(
            "DO $$BEGIN CREATE ROLE rds_replication; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END$$"
        )
        conn.execute(
            sql.SQL("GRANT rds_replication TO {} WITH ADMIN OPTION").format(
                sql.Identifier(cluster.master)
            )
        )
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(cluster.source.database),
                sql.Identifier(cluster.master),
            )
        )
    _seed_source(cluster)
    return cluster


def _seed_source(cluster: Cluster) -> None:
    with pg_client.connect(cluster.master_login(cluster.source)) as conn:
        conn.execute(_SOURCE_SCHEMA)
        app = sql.Identifier(cluster.source_app)
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(app))
        conn.execute(
            sql.SQL("GRANT SELECT, INSERT ON public.orders TO {}").format(app)
        )
        conn.execute(
            sql.SQL(
                "GRANT USAGE ON SEQUENCE public.orders_id_seq TO {}"
            ).format(app)
        )
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA shop TO {}").format(app))
        conn.execute(sql.SQL("GRANT SELECT ON shop.items TO {}").format(app))


def drop(cluster: Cluster) -> None:
    with cluster.superuser("postgres") as conn:
        for endpoint in (cluster.source, cluster.target):
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(endpoint.database)
                )
            )
        roles = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname LIKE %s",
            (f"%\\_{cluster.suffix}",),
        ).fetchall()
        # master 가 준 멤버십이 남은 역할을 먼저 지우고 master 를 마지막에.
        for (role,) in sorted(roles, key=lambda r: r[0] == cluster.master):
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
