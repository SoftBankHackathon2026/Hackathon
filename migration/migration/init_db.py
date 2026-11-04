"""관리자 계정으로 migration_demo DB 생성, 001_demo.sql 적용, 역할 LOGIN·비밀번호·복제 권한 설정. 재실행 안전."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from . import db as dbmod
from .config import Config, ConfigError, load_config, migration_home

log = logging.getLogger("migration.init_db")

ROLE_KEYS: tuple[str, ...] = ("app", "cdc", "sink", "verify")
PROTECTED_DATABASES: frozenset[str] = frozenset({"postgres", "template0", "template1", "oneaction_review"})


def default_sql_path() -> Path:
    return migration_home() / "sql" / "001_demo.sql"


def ensure_database(admin_conn: psycopg.Connection[Any], dbname: str) -> bool:
    """없으면 CREATE DATABASE. 만들었으면 True. autocommit 연결이어야 한다."""
    if dbname in PROTECTED_DATABASES:
        raise ConfigError(f"보호된 데이터베이스 이름이다: {dbname}")
    with admin_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
        if cur.fetchone():
            log.info("데이터베이스 %s 이미 존재", dbname)
            return False
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    log.info("데이터베이스 %s 생성", dbname)
    return True


def apply_sql_file(conn: psycopg.Connection[Any], path: Path) -> None:
    """스크립트 전체를 한 트랜잭션으로 적용한다."""
    script = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(script)
    conn.commit()
    log.info("SQL 적용: %s", path)


def grant_connect(conn: psycopg.Connection[Any], dbname: str, roles: list[str]) -> None:
    """GRANT CONNECT ON DATABASE. DB 이름이 설정에 따라 달라 SQL 파일 대신 여기서 수행한다."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(dbname), sql.SQL(", ").join(sql.Identifier(r) for r in roles)))
    conn.commit()
    log.info("GRANT CONNECT ON DATABASE %s TO %s", dbname, ", ".join(roles))


def set_role_login(conn: psycopg.Connection[Any], role: str, password: str) -> None:
    """LOGIN 과 비밀번호를 설정한다. 비밀번호는 로그에 남기지 않는다."""
    with conn.cursor() as cur:
        cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
    conn.commit()
    log.info("역할 %s: LOGIN + 비밀번호 설정", role)


def grant_replication(conn: psycopg.Connection[Any], role: str, *, managed_rds: bool) -> None:
    """온프렘은 REPLICATION 속성, RDS 는 rds_replication 역할을 준다 (있을 때만)."""
    with conn.cursor() as cur:
        if managed_rds:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = 'rds_replication'")
            if cur.fetchone():
                cur.execute(sql.SQL("GRANT rds_replication TO {}").format(sql.Identifier(role)))
                log.info("역할 %s: rds_replication 부여", role)
            else:
                log.info("rds_replication 역할이 없어 복제 권한을 건너뜀 (귀환 방향에서만 필요)")
        else:
            cur.execute(sql.SQL("ALTER ROLE {} WITH REPLICATION").format(sql.Identifier(role)))
            log.info("역할 %s: REPLICATION 속성 부여", role)
    conn.commit()


def set_app_write(conn: psycopg.Connection[Any], app_role: str, table: str, *, allow: bool) -> None:
    """앱 writer 의 INSERT 를 열거나 닫는다. 대상 DB 는 sink 만 쓰도록 닫은 채 시작한다."""
    verb = sql.SQL("GRANT INSERT ON {} TO {}") if allow else sql.SQL("REVOKE INSERT ON {} FROM {}")
    with conn.cursor() as cur:
        cur.execute(verb.format(dbmod.table_ident(table), sql.Identifier(app_role)))
    conn.commit()
    log.info("앱 계정 %s INSERT %s", app_role, "허용" if allow else "차단 (전환 시 cutover 가 GRANT)")


def check_logical_decoding(conn: psycopg.Connection[Any]) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name, setting FROM pg_settings WHERE name IN ('wal_level', 'max_replication_slots', "
                    "'max_wal_senders', 'ssl')")
        settings = {str(n): str(s) for n, s in cur.fetchall()}
    conn.commit()
    return settings


def run(config: Config, target_name: str, *, sql_path: Path, env: dict[str, str] | None = None,
        app_write: str = "auto") -> int:
    """app_write: auto(job.source 만 허용) | allow | deny."""
    env = dict(os.environ) if env is None else env
    target = config.target(target_name)
    # RDS 는 REPLICATION 속성을 직접 줄 수 없다. targets.<name>.kind 가 있으면 그걸 따르고,
    # 없으면 종전처럼 job.source 가 아닌 쪽을 RDS 로 추정한다 (귀환처럼 AWS 가 source 면 kind: rds 를 적는다).
    managed_rds = (target.kind == "rds") if target.kind else (target_name != config.job.source)
    allow_app_write = {"auto": target_name == config.job.source, "allow": True, "deny": False}[app_write]
    admin_maint = config.connection(target_name, "admin", dbname=target.admin_dbname, env=env,
                                    application_name="migration-init")
    log.info("관리자 접속 %s", admin_maint)
    with dbmod.connect(admin_maint, autocommit=True) as conn:
        ensure_database(conn, target.dbname)

    admin_db = config.connection(target_name, "admin", env=env, application_name="migration-init")
    with dbmod.connect(admin_db, statement_timeout_ms=120_000) as conn:
        ident = dbmod.server_identity(conn)
        log.info("접속 확인 db=%s addr=%s version=%s", ident.database, ident.server_addr, ident.version.split(",")[0])
        apply_sql_file(conn, sql_path)
        grant_connect(conn, target.dbname, [target.user_spec(k).user for k in ROLE_KEYS])
        for role_key in ROLE_KEYS:
            spec = target.user_spec(role_key)
            password = env.get(spec.password_env)
            if not password:
                raise ConfigError(f"환경변수 {spec.password_env} 가 비어 있다 (역할 {spec.user})")
            set_role_login(conn, spec.user, password)
        grant_replication(conn, target.user_spec("cdc").user, managed_rds=managed_rds)
        set_app_write(conn, target.user_spec("app").user, config.job.table, allow=allow_app_write)
        settings = check_logical_decoding(conn)
        log.info("서버 설정: %s", settings)
        if not managed_rds and settings.get("wal_level") != "logical":
            log.warning("wal_level=%s: 논리 디코딩에는 'logical' 이 필요하다. 재시작이 필요한 설정이다",
                        settings.get("wal_level"))
        if managed_rds:
            log.info("target=%s 은 publication 을 만들지 않는다 (정방향 sink 대상)", target_name)
        for role_key in ROLE_KEYS:
            user = target.user_spec(role_key).user
            privs = {p: dbmod.has_table_privilege(conn, user, config.job.table, p)
                     for p in ("SELECT", "INSERT", "UPDATE", "DELETE")}
            log.info("권한 %s: %s", user, {k: v for k, v in privs.items() if v})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="migration_demo DB·스키마·역할 초기화 (재실행 안전)")
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG", "config.yaml"))
    p.add_argument("--target", required=True, choices=None, help="onprem | aws")
    p.add_argument("--sql", default=None, help=f"기본: {default_sql_path()}")
    p.add_argument("--app-write", choices=("auto", "allow", "deny"), default="auto",
                   help="앱 계정 INSERT. auto: job.source 에서만 허용, 대상은 차단 (기본)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
        sql_path = Path(args.sql) if args.sql else default_sql_path()
        if not sql_path.exists():
            log.error("SQL 파일이 없다: %s", sql_path)
            return 2
        return run(config, args.target, sql_path=sql_path, app_write=args.app_write)
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except psycopg.Error as exc:
        log.error("DB 오류: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
