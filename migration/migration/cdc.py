"""Kafka Connect REST 클라이언트와 Debezium 커넥터 준비(publication·slot)·템플릿 렌더링."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

import psycopg
from psycopg import sql

from . import db as dbmod
from .config import Config, ConfigError, load_config, migration_home, redact

log = logging.getLogger("migration.cdc")

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
SECRET_KEYS = ("password", "secret", "token")


class ConnectError(RuntimeError):
    """Kafka Connect REST 호출 실패."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def mask_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """로그용: 비밀번호 계열 키를 가린다."""
    return {k: ("***" if any(s in k.lower() for s in SECRET_KEYS) else v) for k, v in config.items()}


def render_template(text: str, variables: Mapping[str, Any]) -> str:
    """`{{name}}` 자리표시자를 치환한다. Connect 의 `${file:...}` 는 건드리지 않는다."""
    missing: set[str] = set()

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in variables:
            missing.add(key)
            return m.group(0)
        return str(variables[key])

    out = PLACEHOLDER.sub(sub, text)
    if missing:
        raise ConfigError(f"템플릿 변수 누락: {sorted(missing)}")
    return out


def template_variables(config: Config) -> dict[str, Any]:
    """작업 설정에서 템플릿 변수 사전을 만든다. 비밀번호는 포함하지 않는다."""
    job = config.job
    src, tgt = config.target(job.source), config.target(job.target)
    return {
        "job_id": job.job_id,
        "table": job.table,
        "topic": job.topic,
        "secrets_file": config.kafka.secrets_file,
        "source_host": src.host, "source_port": src.port, "source_dbname": src.dbname,
        "source_sslmode": src.sslmode, "source_cdc_user": src.user_spec("cdc").user,
        "target_host": tgt.host, "target_port": tgt.port, "target_dbname": tgt.dbname,
        "target_sslmode": tgt.sslmode, "target_sink_user": tgt.user_spec("sink").user,
    }


def load_connector_template(path: Path, variables: Mapping[str, Any]) -> dict[str, Any]:
    rendered = render_template(path.read_text(encoding="utf-8"), variables)
    try:
        doc = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"템플릿 JSON 파싱 실패 {path}: {exc}") from None
    if not isinstance(doc, dict) or "name" not in doc or not isinstance(doc.get("config"), dict):
        raise ConfigError(f"템플릿은 {{name, config}} 형식이어야 한다: {path}")
    return doc


def default_templates(direction_prefix: str = "forward") -> dict[str, Path]:
    base = migration_home() / "connectors"
    return {"source": base / f"{direction_prefix}-source.json.tmpl", "sink": base / f"{direction_prefix}-sink.json.tmpl"}


class ConnectClient:
    """Kafka Connect REST API 의 최소 클라이언트 (urllib, 표준 라이브러리)."""

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - 설정된 내부 URL
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ConnectError(redact(f"{method} {path} → HTTP {exc.code}: {detail}"), status=exc.code) from None
        except urllib.error.URLError as exc:
            raise ConnectError(f"{method} {path} 연결 실패: {exc.reason}") from None

    def list_connectors(self) -> list[str]:
        return list(self._request("GET", "/connectors") or [])

    def exists(self, name: str) -> bool:
        try:
            self._request("GET", f"/connectors/{name}")
            return True
        except ConnectError as exc:
            if exc.status == 404:
                return False
            raise

    def get_config(self, name: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/connectors/{name}/config") or {})

    def put_config(self, name: str, config: Mapping[str, Any]) -> dict[str, Any]:
        """생성 또는 갱신 (멱등)."""
        return dict(self._request("PUT", f"/connectors/{name}/config", dict(config)) or {})

    def status(self, name: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/connectors/{name}/status") or {})

    def pause(self, name: str) -> None:
        self._request("PUT", f"/connectors/{name}/pause")

    def resume(self, name: str) -> None:
        self._request("PUT", f"/connectors/{name}/resume")

    def stop(self, name: str) -> None:
        """Connect 3.5+ 의 STOPPED 상태. 오래된 버전은 pause 로 대체한다."""
        try:
            self._request("PUT", f"/connectors/{name}/stop")
        except ConnectError as exc:
            if exc.status in (404, 405):
                log.warning("/stop 미지원 (HTTP %s) → pause 로 대체", exc.status)
                self.pause(name)
            else:
                raise

    def delete(self, name: str) -> bool:
        """삭제. 이미 없으면 False."""
        try:
            self._request("DELETE", f"/connectors/{name}")
            return True
        except ConnectError as exc:
            if exc.status == 404:
                return False
            raise

    def offsets(self, name: str) -> dict[str, Any] | None:
        """Connect 3.6+ 의 /offsets. 미지원이면 None."""
        try:
            return dict(self._request("GET", f"/connectors/{name}/offsets") or {})
        except ConnectError as exc:
            if exc.status in (404, 405):
                return None
            raise

    def is_running(self, name: str) -> tuple[bool, dict[str, Any]]:
        """커넥터와 모든 task 가 RUNNING 인지. 등록 직후에는 /status 가 잠시 404 를 돌려주므로
        그때는 실패가 아니라 '아직 아님'(False) 으로 보고 호출자가 다시 묻게 한다."""
        try:
            st = self.status(name)
        except ConnectError as exc:
            if exc.status == 404:
                return False, {"pending": "status not available yet"}
            raise
        conn_state = (st.get("connector") or {}).get("state")
        tasks = st.get("tasks") or []
        ok = conn_state == "RUNNING" and bool(tasks) and all(t.get("state") == "RUNNING" for t in tasks)
        return ok, st


def sink_kafka_offsets(offsets_doc: Mapping[str, Any] | None) -> dict[str, int]:
    """sink /offsets 응답에서 {topic-partition: next_offset} 을 뽑는다."""
    out: dict[str, int] = {}
    for entry in (offsets_doc or {}).get("offsets") or []:
        part = entry.get("partition") or {}
        off = entry.get("offset") or {}
        topic, partition = part.get("kafka_topic"), part.get("kafka_partition")
        if topic is None or partition is None or "kafka_offset" not in off:
            continue
        out[f"{topic}-{partition}"] = int(off["kafka_offset"])
    return out


# ---- publication / slot 사전 생성 --------------------------------------------


def prepare_source(conn: psycopg.Connection[Any], *, publication: str, slot: str, table: str,
                   owner: str, create_slot: bool = True) -> dict[str, Any]:
    """publication 과 slot 을 없으면 만든다. 멱등."""
    result: dict[str, Any] = {"publication": publication, "slot": slot}
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_publication WHERE pubname = %s", (publication,))
        if cur.fetchone():
            result["publication_created"] = False
        else:
            cur.execute(sql.SQL("CREATE PUBLICATION {} FOR TABLE {}").format(
                sql.Identifier(publication), dbmod.table_ident(table)))
            result["publication_created"] = True
        cur.execute(sql.SQL("ALTER PUBLICATION {} OWNER TO {}").format(sql.Identifier(publication), sql.Identifier(owner)))
        # 논리 슬롯은 쓰기를 수행한 트랜잭션 안에서 만들 수 없으므로 여기서 먼저 커밋한다.
        conn.commit()
        cur.execute("SELECT slot_name, active, plugin FROM pg_replication_slots WHERE slot_name = %s", (slot,))
        row = cur.fetchone()
        if row:
            result["slot_created"] = False
            result["slot_active"] = bool(row[1])
        elif create_slot:
            cur.execute("SELECT pg_create_logical_replication_slot(%s, 'pgoutput')", (slot,))
            result["slot_created"] = True
            result["slot_active"] = False
        else:
            result["slot_created"] = False
            result["slot_active"] = None
    conn.commit()
    return result


def drop_slot(conn: psycopg.Connection[Any], slot: str) -> bool:
    """비활성 슬롯을 지운다. 없으면 False. 활성이면 오류."""
    with conn.cursor() as cur:
        cur.execute("SELECT active FROM pg_replication_slots WHERE slot_name = %s", (slot,))
        row = cur.fetchone()
        if not row:
            conn.commit()
            return False
        if row[0]:
            conn.commit()
            raise ConnectError(f"슬롯 {slot} 이 아직 활성 상태다. 커넥터를 먼저 삭제한다")
        cur.execute("SELECT pg_drop_replication_slot(%s)", (slot,))
    conn.commit()
    return True


# ---- CLI ---------------------------------------------------------------------


def cmd_prepare(config: Config, args: argparse.Namespace) -> int:
    spec = config.connection(config.job.source, "admin", application_name="migration-cdc")
    with dbmod.connect(spec) as conn:
        res = prepare_source(conn, publication=config.job.publication_name, slot=config.job.slot_name,
                             table=config.job.table, owner=config.target(config.job.source).user_spec("cdc").user,
                             create_slot=not args.no_slot)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_render(config: Config, args: argparse.Namespace) -> int:
    variables = template_variables(config)
    docs = {k: load_connector_template(p, variables) for k, p in default_templates(args.direction).items()}
    print(json.dumps({k: {"name": d["name"], "config": mask_config(d["config"])} for k, d in docs.items()},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_apply(config: Config, args: argparse.Namespace) -> int:
    client = ConnectClient(config.kafka.connect_url)
    variables = template_variables(config)
    docs = load_connector_template_pair(args.direction, variables)
    for kind in ("source", "sink"):
        doc = docs[kind]
        client.put_config(doc["name"], doc["config"])
        log.info("커넥터 등록/갱신 %s (%s): %s", doc["name"], kind, mask_config(doc["config"]))
    return 0


def load_connector_template_pair(direction: str, variables: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: load_connector_template(p, variables) for k, p in default_templates(direction).items()}


def cmd_status(config: Config, _args: argparse.Namespace) -> int:
    client = ConnectClient(config.kafka.connect_url)
    out: dict[str, Any] = {}
    for name in (config.job.source_connector, config.job.sink_connector):
        if client.exists(name):
            running, st = client.is_running(name)
            out[name] = {"running": running, "status": st, "offsets": client.offsets(name)}
        else:
            out[name] = {"exists": False}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _connector_names(config: Config, args: argparse.Namespace) -> list[str]:
    if args.which == "source":
        return [config.job.source_connector]
    if args.which == "sink":
        return [config.job.sink_connector]
    return [config.job.source_connector, config.job.sink_connector]


def cmd_pause(config: Config, args: argparse.Namespace) -> int:
    client = ConnectClient(config.kafka.connect_url)
    for name in _connector_names(config, args):
        client.pause(name)
        log.info("pause %s", name)
    return 0


def cmd_stop(config: Config, args: argparse.Namespace) -> int:
    client = ConnectClient(config.kafka.connect_url)
    for name in _connector_names(config, args):
        client.stop(name)
        log.info("stop %s", name)
    return 0


def cmd_delete(config: Config, args: argparse.Namespace) -> int:
    client = ConnectClient(config.kafka.connect_url)
    for name in _connector_names(config, args):
        log.info("delete %s → %s", name, "삭제" if client.delete(name) else "이미 없음")
    return 0


def cmd_drop_slot(config: Config, _args: argparse.Namespace) -> int:
    spec = config.connection(config.job.source, "admin", application_name="migration-cdc")
    with dbmod.connect(spec) as conn:
        dropped = drop_slot(conn, config.job.slot_name)
    log.info("슬롯 %s: %s", config.job.slot_name, "삭제" if dropped else "없음")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Debezium/Kafka Connect 제어")
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG", "config.yaml"))
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    subs = p.add_subparsers(dest="command", required=True)
    sp = subs.add_parser("prepare", help="원본에 publication·slot 사전 생성 (관리자)")
    sp.add_argument("--no-slot", action="store_true", help="slot 은 Debezium 이 만들게 둔다")
    sp.set_defaults(func=cmd_prepare)
    sp = subs.add_parser("render", help="템플릿 렌더링 결과 출력 (비밀번호 가림)")
    sp.add_argument("--direction", default="forward")
    sp.set_defaults(func=cmd_render)
    sp = subs.add_parser("apply", help="Source/Sink 커넥터 등록 또는 갱신")
    sp.add_argument("--direction", default="forward")
    sp.set_defaults(func=cmd_apply)
    subs.add_parser("status", help="커넥터 상태·offset").set_defaults(func=cmd_status)
    for name, func in (("pause", cmd_pause), ("stop", cmd_stop), ("delete", cmd_delete)):
        sp = subs.add_parser(name)
        sp.add_argument("--which", choices=("source", "sink", "both"), default="both")
        sp.set_defaults(func=func)
    subs.add_parser("drop-slot", help="커넥터 삭제 후 비활성 슬롯 제거 (WAL 축적 방지)").set_defaults(func=cmd_drop_slot)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = load_config(args.config)
        return int(args.func(config, args))
    except ConfigError as exc:
        log.error("설정 오류: %s", exc)
        return 2
    except ConnectError as exc:
        log.error("Kafka Connect 오류: %s", exc)
        return 1
    except psycopg.Error as exc:
        log.error("DB 오류: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
