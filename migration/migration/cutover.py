"""전환 상태 머신: PRECHECK → … → COMPLETED. 단계마다 SQLite 에 체크포인트를 남기고, 재실행 시 저장된 단계부터 재개한다."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import psycopg
from psycopg import sql

from . import db as dbmod
from . import verify as verifymod
from .cdc import ConnectClient, ConnectError, load_connector_template_pair, mask_config, prepare_source, \
    sink_kafka_offsets, template_variables
from .config import Config, ConfigError, load_config
from .state import CutoverJob, CutoverStore
from .cutover_policy import CutoverPolicyGate
from .ingress_client import HttpIngressClient, IngressClientError, check_app_ready
from .traffic_client import HttpTrafficClient
from .policy_client import build_check
from .bot_client import HttpBotClient, BotClientError

log = logging.getLogger("migration.cutover")

PRECHECK, COPYING, CATCHING_UP, READY, FENCING, DRAINING = "PRECHECK", "COPYING", "CATCHING_UP", "READY", "FENCING", "DRAINING"
SYNC_CONFIRMED, SWITCHING, VERIFYING, COMPLETED = "SYNC_CONFIRMED", "SWITCHING", "VERIFYING", "COMPLETED"
ABORTED, NEEDS_RECOVERY, ROLLED_BACK = "ABORTED", "NEEDS_RECOVERY", "ROLLED_BACK"

STAGES: tuple[str, ...] = (PRECHECK, COPYING, CATCHING_UP, READY, FENCING, DRAINING, SYNC_CONFIRMED,
                           SWITCHING, VERIFYING, COMPLETED)
TERMINAL: frozenset[str] = frozenset({COMPLETED, ABORTED, NEEDS_RECOVERY, ROLLED_BACK})
# 이 단계부터는 대상 DB 에 앱 쓰기를 열었으므로 실패 시 자동 원복하지 않는다.
POINT_OF_NO_RETURN = SWITCHING

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_WAITING_APPROVAL = 0, 1, 2, 3


class CutoverError(RuntimeError):
    """단계 실패. 상태 머신이 ABORTED/NEEDS_RECOVERY 로 전이한다."""


class ApprovalRequired(RuntimeError):
    """READY 에서 승인 없이 진행하려 할 때."""


# ---- 포트 (외부 시스템 추상화; 테스트에서 대체) --------------------------------


class BotPort(Protocol):
    def status(self) -> dict[str, Any]: ...
    def set_generation(self, enabled: bool, *, expected_run_id: str, expected_ledger_id: str,
                       limit: int | None = None) -> dict[str, Any]: ...


class ConnectPort(Protocol):
    def list_connectors(self) -> list[str]: ...
    def exists(self, name: str) -> bool: ...
    def get_config(self, name: str) -> dict[str, Any]: ...
    def put_config(self, name: str, config: dict[str, Any]) -> dict[str, Any]: ...
    def status(self, name: str) -> dict[str, Any]: ...
    def is_running(self, name: str) -> tuple[bool, dict[str, Any]]: ...
    def delete(self, name: str) -> bool: ...
    def offsets(self, name: str) -> dict[str, Any] | None: ...


class DbPort(Protocol):
    """관리자 권한 DB 작업. target 인자는 설정의 target 이름."""

    def precheck(self, target: str, *, is_source: bool) -> dict[str, Any]: ...
    def app_can_insert(self, target: str) -> bool: ...
    def revoke_app_insert(self, target: str) -> None: ...
    def grant_app_insert(self, target: str) -> None: ...
    def terminate_app_sessions(self, target: str) -> int: ...
    def test_insert_rejected(self, target: str, request_id: str) -> bool: ...
    def count_rows(self, target: str, phase: str | None = None) -> int: ...
    def max_created_at(self, target: str) -> str | None: ...
    def count_rows_in_scope(self, target: str, *, since: str | None = None, upto: str | None = None) -> int: ...
    def row_exists(self, target: str, request_id: str) -> bool: ...
    def latest_ids(self, target: str, n: int, *, since: str | None = None) -> list[str]: ...
    def prepare_source(self, target: str) -> dict[str, Any]: ...
    def drop_source_slot(self, target: str) -> dict[str, Any]: ...


class VerifyPort(Protocol):
    def run(self, mode: str, label: str, *, since: str | None = None, upto: str | None = None,
            method: str = "rows") -> dict[str, Any]: ...


class TrafficPort(Protocol):
    def status(self) -> dict[str, Any]: ...
    def switch(self, target: str) -> dict[str, Any]: ...


class IngressPort(Protocol):
    """접수 서비스(durable-ingress) 제어. pause → target → resume 한다."""

    def status(self) -> dict[str, Any]: ...
    def pause(self) -> dict[str, Any]: ...
    def set_target(self, target: str, expected_epoch: str) -> dict[str, Any]: ...
    def resume(self, expected_epoch: str) -> dict[str, Any]: ...
    def check_app(self, job_target: str) -> dict[str, Any]: ...


# ---- 실제 어댑터 -----------------------------------------------------------------


class PgAdminPort:
    """psycopg 로 관리자 작업을 수행한다. 앱 계정 차단·시험 INSERT 도 여기서."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.table = config.job.table
        self._pools = {}

    def _admin(self, target: str):
        if target not in self._pools:
            self._pools[target] = dbmod.connection_pool(
                self.config.connection(target, "admin", application_name="migration-cutover"))
        return self._pools[target].connection()

    def close(self) -> None:
        for pool in self._pools.values():
            pool.close()
        self._pools.clear()

    def _app_user(self, target: str) -> str:
        return self.config.target(target).user_spec("app").user

    def precheck(self, target: str, *, is_source: bool) -> dict[str, Any]:
        with self._admin(target) as conn:
            ident = dbmod.server_identity(conn)
            info: dict[str, Any] = {"identity": ident.as_dict(), "table_exists": dbmod.table_exists(conn, self.table)}
            if not info["table_exists"]:
                return info
            app = self._app_user(target)
            info["app_insert"] = dbmod.has_table_privilege(conn, app, self.table, "INSERT")
            info["app_select"] = dbmod.has_table_privilege(conn, app, self.table, "SELECT")
            info["row_count"] = dbmod.count_rows(conn, self.table)
            with conn.cursor() as cur:
                cur.execute("SELECT setting FROM pg_settings WHERE name = 'wal_level'")
                row = cur.fetchone()
                info["wal_level"] = row[0] if row else None
                if is_source:
                    cdc = self.config.target(target).user_spec("cdc").user
                    # RDS 는 REPLICATION 속성 대신 rds_replication 역할 멤버십으로 권한을 주므로
                    # rolreplication 만 보면 귀환(AWS 원본) PRECHECK 가 실패한다. 둘 중 하나면 통과.
                    # rds_replication 역할이 없는 일반 PostgreSQL 에서도 오류 없이 false 가 되도록
                    # pg_has_role 을 역할 존재 여부로 감싼다.
                    cur.execute(
                        """
                        SELECT r.rolreplication
                               OR EXISTS (
                                   SELECT 1 FROM pg_roles g
                                   WHERE g.rolname = 'rds_replication'
                                     AND pg_has_role(r.oid, g.oid, 'MEMBER')
                               )
                        FROM pg_roles r WHERE r.rolname = %s
                        """,
                        (cdc,),
                    )
                    row = cur.fetchone()
                    info["cdc_replication"] = bool(row and row[0])
            conn.commit()
        return info

    def app_can_insert(self, target: str) -> bool:
        with self._admin(target) as conn:
            return dbmod.has_table_privilege(conn, self._app_user(target), self.table, "INSERT")

    def revoke_app_insert(self, target: str) -> None:
        with self._admin(target) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("REVOKE INSERT ON {} FROM {}").format(
                    dbmod.table_ident(self.table), sql.Identifier(self._app_user(target))))
            conn.commit()

    def grant_app_insert(self, target: str) -> None:
        with self._admin(target) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("GRANT INSERT ON {} TO {}").format(
                    dbmod.table_ident(self.table), sql.Identifier(self._app_user(target))))
            conn.commit()

    def terminate_app_sessions(self, target: str) -> int:
        """앱 계정의 세션만 종료한다. CDC·sink·관리 세션은 건드리지 않는다."""
        with self._admin(target) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                    "WHERE usename = %s AND datname = current_database() AND pid <> pg_backend_pid()",
                    (self._app_user(target),),
                )
                row = cur.fetchone()
            conn.commit()
        return int(row[0]) if row else 0

    def test_insert_rejected(self, target: str, request_id: str) -> bool:
        """앱 계정으로 실제 INSERT 를 시도해 권한 거절을 확인한다. 성공하면(차단 실패) 즉시 롤백한다."""
        spec = self.config.connection(target, "app", application_name="migration-fence-test")
        try:
            conn = dbmod.connect(spec)
        except psycopg.OperationalError as exc:
            # 접속 자체가 거부되면 쓰기도 불가능하지만, 권한 차단의 증거로는 약하므로 로그로 남기고 거절로 본다.
            log.warning("앱 계정 접속 불가 (차단 확인은 통과로 간주): %s", exc)
            return True
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        sql.SQL("INSERT INTO {} (request_id, run_id, phase, request_no, payload, created_at) "
                                "VALUES (%s, 'fence-test', 'fence_test', 0, 'fence test', now())")
                        .format(dbmod.table_ident(self.table)),
                        (request_id,),
                    )
                except psycopg.errors.InsufficientPrivilege:
                    conn.rollback()
                    return True
            conn.rollback()  # 차단 실패: 시험 행을 남기지 않는다
            return False
        finally:
            conn.close()

    def count_rows(self, target: str, phase: str | None = None) -> int:
        with self._admin(target) as conn:
            return dbmod.count_rows(conn, self.table, phase)

    def row_exists(self, target: str, request_id: str) -> bool:
        with self._admin(target) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT 1 FROM {} WHERE request_id = %s").format(dbmod.table_ident(self.table)),
                            (request_id,))
                return cur.fetchone() is not None

    def latest_ids(self, target: str, n: int, *, since: str | None = None) -> list[str]:
        with self._admin(target) as conn:
            return dbmod.latest_ids_in_scope(conn, self.table, n, since=verifymod.parse_ts(since))

    def max_created_at(self, target: str) -> str | None:
        with self._admin(target) as conn:
            return verifymod.format_ts(dbmod.max_created_at(conn, self.table))

    def count_rows_in_scope(self, target: str, *, since: str | None = None, upto: str | None = None) -> int:
        with self._admin(target) as conn:
            return dbmod.count_rows_in_scope(conn, self.table, since=verifymod.parse_ts(since),
                                             upto=verifymod.parse_ts(upto))

    def prepare_source(self, target: str) -> dict[str, Any]:
        job = self.config.job
        with self._admin(target) as conn:
            return prepare_source(conn, publication=job.publication_name, slot=job.slot_name, table=job.table,
                                  owner=self.config.target(target).user_spec("cdc").user)

    def drop_source_slot(self, target: str) -> dict[str, Any]:
        """Use admin for the slot and the configured CDC owner for publication."""
        from .source_cleanup import cleanup_source
        return cleanup_source(self.config, target)



class VerifyAdapter:
    def __init__(self, config: Config, state_dir: Path, report_dir: Path) -> None:
        self.config, self.state_dir, self.report_dir = config, state_dir, report_dir

    def run(self, mode: str, label: str, *, since: str | None = None, upto: str | None = None,
            method: str = "rows") -> dict[str, Any]:
        path = self.report_dir / f"verify-{self.config.job.job_id}-{label}.json"
        cmp = verifymod.run(self.config, source=self.config.job.source, target=self.config.job.target,
                            state_dir=None, mode=mode, report_path=path, extra_meta={"label": label},
                            since=since, upto=upto, method=method)
        return {"passed": cmp.passed, "counts": cmp.counts, "report": str(path), "method": cmp.method,
                "scope": {"since": since, "upto": upto}, "mismatched_bucket_count": len(cmp.mismatched_buckets),
                "source_count": cmp.source_count, "target_count": cmp.target_count}


# ---- 상태 머신 --------------------------------------------------------------------


class IngressAdapter:
    """설정의 ingress 절로 HttpIngressClient 와 대상 앱 readiness 검사를 묶는다."""

    def __init__(self, config: Config) -> None:
        self.cfg = config.ingress
        assert self.cfg.control_url
        self.client = HttpIngressClient(self.cfg.control_url, token_env=self.cfg.token_env,
                                        timeout=self.cfg.timeout_seconds)

    def status(self) -> dict[str, Any]:
        return self.client.status()

    def pause(self) -> dict[str, Any]:
        return self.client.pause()

    def set_target(self, target: str, expected_epoch: str) -> dict[str, Any]:
        return self.client.set_target(target, expected_epoch)

    def resume(self, expected_epoch: str) -> dict[str, Any]:
        return self.client.resume(expected_epoch)

    def check_app(self, job_target: str) -> dict[str, Any]:
        check = self.cfg.app_checks.get(job_target)
        if check is None:
            return {"skipped": True, "ok": True, "problems": []}
        return check_app_ready(check.url, environment=check.environment, database=check.database,
                               token_env=check.token_env, timeout=self.cfg.timeout_seconds)


@dataclass
class Ports:
    bot: BotPort
    connect: ConnectPort
    db: DbPort
    verify: VerifyPort
    ingress: IngressPort | None = None   # None 이면 접수 큐는 운영자가 수동으로 제어한다
    traffic: TrafficPort | None = None


class Cutover:
    """job_id 하나의 전환을 수행한다. 같은 job_id 로 재실행해도 안전하다."""

    def __init__(
        self,
        config: Config,
        store: CutoverStore,
        ports: Ports,
        *,
        connector_docs: dict[str, dict[str, Any]],
        approve: str | None = None,
        policy_gate: Callable[..., dict[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.store = store
        self.p = ports
        self.docs = connector_docs
        self.approve = approve
        self.policy_gate = policy_gate or CutoverPolicyGate(config, Path(config.cutover.state_dir))
        self.sleep = sleep
        self.clock = clock
        self.job_id = config.job.job_id
        self.source = config.job.source
        self.target = config.job.target
        self.cc = config.cutover
        self.job = store.load(self.job_id) or CutoverJob(
            job_id=self.job_id, direction=f"{self.source}->{self.target}", source=self.source,
            target=self.target, stage=PRECHECK, checkpoint={"grant_tracking_version": 1, "writer_mode": "traffic_bot_v1"}, updated_at="")
        if (self.job.source, self.job.target) != (self.source, self.target):
            raise ConfigError(f"저장된 작업 {self.job_id} 의 방향({self.job.direction})이 설정과 다르다")

    def _policy_guard(self, stage: str, checks: dict[str, Any] | None = None) -> None:
        result = self.policy_gate(stage, checks)
        saved = self.job.checkpoint.get("policy_binding")
        binding = {key: result.get(key) for key in ("plan_digest", "execution_digest")}
        if saved and saved != binding:
            raise CutoverError("정책 계획 또는 실행 설정이 저장된 작업과 다르다")
        self._save(policy_binding=binding, last_policy=result)

    # ---- 저장 -----

    def _save(self, stage: str | None = None, **checkpoint: Any) -> None:
        cp = dict(self.job.checkpoint)
        cp.update(checkpoint)
        self.job = CutoverJob(job_id=self.job.job_id, direction=self.job.direction, source=self.source,
                              target=self.target, stage=stage or self.job.stage, checkpoint=cp, updated_at="")
        self.store.save(self.job)

    def _step(self, name: str, action: Callable[[], Any]) -> Any:
        """외부 작업 한 번만. 이미 했으면 기록을 돌려주고 재실행하지 않는다."""
        done = self.store.step_done(self.job_id, name)
        if done is not None:
            log.info("[%s] 이미 수행됨, 건너뜀: %s", name, done)
            return done.get("result")
        result = action()
        self.store.mark_step(self.job_id, name, {"result": result})
        log.info("[%s] 완료: %s", name, json.dumps(result, ensure_ascii=False, default=str)[:500])
        return result

    def _wait(self, label: str, predicate: Callable[[], tuple[bool, dict[str, Any]]], timeout: float | None = None) -> dict[str, Any]:
        deadline = self.clock() + (timeout or self.cc.stage_timeout_seconds)
        while True:
            ok, info = predicate()
            if ok:
                log.info("[%s] 조건 충족: %s", label, json.dumps(info, ensure_ascii=False, default=str)[:500])
                return info
            if self.clock() >= deadline:
                raise CutoverError(f"{label} 시간 초과: {info}")
            log.info("[%s] 대기 중: %s", label, json.dumps(info, ensure_ascii=False, default=str)[:300])
            self.sleep(self.cc.poll_interval_seconds)

    def _connectors_healthy(self) -> tuple[bool, dict[str, Any]]:
        out: dict[str, Any] = {}
        healthy = True
        for name in (self.config.job.source_connector, self.config.job.sink_connector):
            running, st = self.p.connect.is_running(name)
            states = [(st.get("connector") or {}).get("state")] + [t.get("state") for t in st.get("tasks") or []]
            out[name] = states
            if "FAILED" in states:
                raise CutoverError(f"커넥터 {name} FAILED: {st}")
            healthy = healthy and running
        return healthy, out

    # ---- 기준점(watermark) -----

    @property
    def watermark(self) -> str | None:
        """COPYING 시작 때 저장한 원본 max(created_at). None 이면 시작 당시 빈 테이블이라 모든 행이 delta 다."""
        if "watermark" not in self.job.checkpoint:
            # 기준점 도입 전에 시작한 작업을 이어서 실행하는 경우. 전체를 delta 로 보고 비교한다(느리지만 안전).
            log.warning("체크포인트에 기준점(watermark)이 없다: 전체 행을 delta 로 비교한다")
            return None
        return self.job.checkpoint["watermark"]

    def _verify_snapshot_set(self, label: str) -> dict[str, Any]:
        """기준점 이전(스냅샷) 집합을 digest 로 비교한다. 기준점이 없으면(빈 테이블 시작) 건너뛴다."""
        if self.watermark is None:
            return {"skipped": True, "passed": True, "reason": "빈 테이블에서 시작: 스냅샷 집합 없음"}
        return self.p.verify.run("pre", label, upto=self.watermark, method=self.cc.snapshot_compare)

    def _count_check(self, label: str, *, allow_target_extra: bool = False) -> dict[str, Any]:
        """전환 구간(원본 쓰기 차단 뒤) 판정. 행을 읽지 않고 기준점 이후(delta)·이전(스냅샷) 건수만 양쪽에서 센다.
        allow_target_extra: 트래픽이 이미 대상으로 간 뒤라 대상 delta 가 더 많아도 되는 경우(전환 후 확인)."""
        wm = self.watermark
        delta_src = self.p.db.count_rows_in_scope(self.source, since=wm)
        delta_tgt = self.p.db.count_rows_in_scope(self.target, since=wm)
        pre_src = self.p.db.count_rows_in_scope(self.source, upto=wm) if wm is not None else 0
        pre_tgt = self.p.db.count_rows_in_scope(self.target, upto=wm) if wm is not None else 0
        delta_ok = delta_tgt >= delta_src if allow_target_extra else delta_src == delta_tgt
        info: dict[str, Any] = {"label": label, "method": "counts", "watermark": wm,
                                "delta_source": delta_src, "delta_target": delta_tgt,
                                "snapshot_source": pre_src, "snapshot_target": pre_tgt,
                                "passed": bool(delta_ok and pre_src == pre_tgt)}
        log.info("건수 확인 %s: delta %d/%d · 스냅샷 %d/%d → %s", label, delta_src, delta_tgt, pre_src, pre_tgt,
                 "일치" if info["passed"] else "불일치")
        return info

    # ---- 접수 서비스 -----

    @property
    def ingress(self) -> IngressPort | None:
        return self.p.ingress

    def _ingress_name(self, job_target: str) -> str:
        return self.config.ingress.ingress_target(job_target)

    def _ingress_snapshot(self) -> dict[str, Any]:
        st = self.ingress.status()  # type: ignore[union-attr]
        return {k: st.get(k) for k in ("mode", "target", "epoch", "in_flight", "last_error")}

    def _route_name(self, job_target: str) -> str:
        return self.config.traffic.route_target(job_target)

    def _bot_snapshot(self) -> dict[str, Any]:
        st = self.p.bot.status()
        for key in ("run_id", "ledger_id"):
            if not isinstance(st.get(key), str) or not st[key]:
                raise CutoverError(f"트래픽봇 {key} 누락: 제어 API 이미지 갱신 필요")
        for key in ("generated_total", "pending", "committed", "failed", "unknown"):
            if type(st.get(key)) is not int or not 0 <= st[key] <= 2**53 - 1:
                raise CutoverError(f"트래픽봇 {key} 상태가 유효하지 않다")
        if st.get("generation") not in ("running", "stopped") or st.get("control_enabled") is not True:
            raise CutoverError("트래픽봇 생성 제어가 활성화되어야 한다")
        if st["unknown"]:
            raise CutoverError("트래픽봇 커밋 여부 미확인 요청이 남아 있다")
        if st.get("fatal") or st.get("paused") is not False:
            raise CutoverError("트래픽봇 전달이 중지되거나 오류 상태다")
        if st.get("request_endpoint") != self.config.bot.request_endpoint:
            raise CutoverError("트래픽봇 요청 경로가 설정한 접수 큐와 다르다")
        identity = self.job.checkpoint.get("bot_identity")
        if identity and any(st[k] != identity[k] for k in ("run_id", "ledger_id")):
            raise CutoverError("트래픽봇 원장/run_id 변경: 기존 작업 자동 재개 불가")
        if st["failed"] > self.job.checkpoint.get("bot_failed_before", st["failed"]):
            raise CutoverError("이전 중 트래픽봇 실패 요청 발생")
        return st

    def _set_bot_generation(self, enabled: bool, limit: int | None = None) -> dict[str, Any]:
        # Read identity even on stop. Never control a replacement bot using old checkpoints.
        st = self.p.bot.status()
        identity = self.job.checkpoint.get("bot_identity")
        if not identity or any(st.get(k) != identity[k] for k in ("run_id", "ledger_id")):
            raise CutoverError("트래픽봇 원장/run_id 변경: 제어 거부")
        out = self.p.bot.set_generation(enabled, expected_run_id=identity["run_id"],
                                        expected_ledger_id=identity["ledger_id"], limit=limit)
        if not enabled and out.get("generation") != "stopped":
            raise CutoverError("트래픽봇 생성 정지 확인 실패")
        return out

    def _start_bot_batch(self, label: str, count: int) -> None:
        key = f"bot_{label}_limit"
        st = self._bot_snapshot()
        target = self.source if label == "source" else self.target
        snap = self._ingress_snapshot()
        if snap["target"] != self._ingress_name(target) or snap["mode"] != "running":
            raise CutoverError("트래픽봇 시작 전 접수 큐 경로/실행 상태 불일치")
        if not self.p.db.app_can_insert(target):
            raise CutoverError("트래픽봇 대상 앱 INSERT 권한이 없다")
        if key not in self.job.checkpoint:
            self._save(**{key: st["generated_total"] + count})
        limit = self.job.checkpoint[key]
        if st["generated_total"] > limit:
            raise CutoverError("트래픽봇 생성 건수가 저장된 한도를 초과했다")
        self._set_bot_generation(st["generated_total"] < limit, limit=limit)

    def _wait_bot_settled(self) -> dict[str, Any]:
        def settled() -> tuple[bool, dict[str, Any]]:
            st = self._bot_snapshot()
            return st["generation"] == "stopped" and st["pending"] == 0, st
        return self._wait("traffic_bot_settled", settled)

    def _wait_bot_batch(self, label: str) -> dict[str, Any]:
        st = self._wait_bot_settled()
        if st["generated_total"] != self.job.checkpoint[f"bot_{label}_limit"]:
            raise CutoverError("트래픽봇이 지정한 요청 건수에 도달하기 전에 중지됐다")
        return st

    def _app_ready(self, target: str) -> tuple[bool, dict[str, Any]]:
        info = self.ingress.check_app(target)
        return bool(info.get("ok")) and not info.get("skipped"), info

    # ---- 단계 -----

    def stage_precheck(self) -> None:
        src = self.p.db.precheck(self.source, is_source=True)
        tgt = self.p.db.precheck(self.target, is_source=False)
        problems: list[str] = []
        if not src.get("table_exists"):
            problems.append(f"원본 {self.source} 에 테이블이 없다")
        if not tgt.get("table_exists"):
            problems.append(f"대상 {self.target} 에 테이블이 없다")
        if src.get("table_exists") and not src.get("app_insert"):
            problems.append("원본 앱 계정에 INSERT 권한이 없다 (아직 차단 전이어야 한다)")
        if tgt.get("table_exists") and tgt.get("app_insert"):
            problems.append("대상 앱 계정에 이미 INSERT 권한이 있다 (sink 만 써야 한다)")
        if src.get("wal_level") not in (None, "logical"):
            problems.append(f"원본 wal_level={src.get('wal_level')} (logical 필요)")
        if src.get("cdc_replication") is False:
            problems.append("원본 CDC 계정에 REPLICATION 권한이 없다")
        if self.p.bot is None or self.ingress is None:
            raise CutoverError("트래픽봇과 접수 큐 제어 설정이 모두 필요하다")
        bs = self._bot_snapshot()
        if self.cc.demo_total_rows is not None:
            if (src.get("row_count") != 800 or tgt.get("row_count") != 0
                    or self.p.db.count_rows(self.source, "seed") != 800):
                problems.append("1000-row demo requires source=800 seed rows and an empty target")
            if bs["generation"] != "stopped" or bs["generated_total"] != 0 or bs["pending"] or bs["failed"]:
                problems.append("1000-row demo requires a fresh stopped traffic bot")
        connectors = self.p.connect.list_connectors()
        ingress_info: dict[str, Any] | None = None
        if self.ingress is not None:
            try:
                src_name, tgt_name = self._ingress_name(self.source), self._ingress_name(self.target)
            except ConfigError as exc:
                raise CutoverError(f"PRECHECK 실패: {exc}") from exc
            ingress_info = self._ingress_snapshot()
            if ingress_info.get("target") != src_name:
                problems.append(f"접수 서비스 target={ingress_info.get('target')} 이 원본({src_name}) 이 아니다")
            ingress_info["expected"] = {"source": src_name, "target": tgt_name}
        traffic_info: dict[str, Any] | None = None
        if self.p.traffic is not None:
            try:
                src_route, tgt_route = self._route_name(self.source), self._route_name(self.target)
            except ConfigError as exc:
                raise CutoverError(f"PRECHECK 실패: {exc}") from exc
            traffic_info = {"target": self.p.traffic.status().get("target"),
                            "expected": {"source": src_route, "target": tgt_route}}
            if traffic_info["target"] not in (None, src_route):
                problems.append(f"공개 도메인 경로={traffic_info['target']} 이 원본({src_route}) 이 아니다")
        if problems:
            raise CutoverError("PRECHECK 실패: " + "; ".join(problems))
        self._save(COPYING, bot_identity={"run_id": bs["run_id"], "ledger_id": bs["ledger_id"]},
                   bot_failed_before=bs["failed"], precheck={"source": src, "target": tgt, "bot": bs,
                                     "connectors": connectors, "ingress": ingress_info, "traffic": traffic_info})

    def stage_copying(self) -> None:
        self._policy_guard("pre_start")
        self._set_bot_generation(False)
        snap = self._ingress_snapshot()
        if snap["target"] != self._ingress_name(self.source):
            raise CutoverError("복사 시작 전 접수 큐가 원본을 가리켜야 한다")
        self._wait("source_app_ready", lambda: self._app_ready(self.source))
        if snap["mode"] != "running":
            self.ingress.resume(str(snap["epoch"]))
        self._wait_bot_settled()
        # 기준점은 커넥터 등록(스냅샷 시작) 전에 한 번만 잡는다. 이후 모든 판정은 이 시각 뒤에 만들어진 행만 본다.
        if "watermark" not in self.job.checkpoint:
            wm = self._step("watermark", lambda: {"created_at": self.p.db.max_created_at(self.source)})
            self._save(watermark=wm.get("created_at"))
        self._step("prepare_source", lambda: self.p.db.prepare_source(self.source))
        for kind in ("source", "sink"):
            doc = self.docs[kind]
            self._step(f"register_{kind}", lambda d=doc: {"name": d["name"], "config": mask_config(
                self.p.connect.put_config(d["name"], d["config"]) or d["config"])})
        def snapshot_done() -> tuple[bool, dict[str, Any]]:
            healthy, states = self._connectors_healthy()
            # seed 건수는 매번 다시 읽는다. 단계 시작 때 한 번만 읽으면 seed 를 나중에 넣었을 때
            # (2026-11-05 리허설처럼) 양쪽이 1,000건이어도 영원히 기다리다 타임아웃된다.
            seed_src = self.p.db.count_rows(self.source, "seed")
            seed_tgt = self.p.db.count_rows(self.target, "seed")
            return healthy and seed_tgt == seed_src, {"connectors": states, "seed_source": seed_src, "seed_target": seed_tgt}

        info = self._wait("snapshot", snapshot_done)
        copy_count = self.p.db.count_rows(self.source)
        if self.cc.demo_total_rows is not None and copy_count != 800:
            raise CutoverError("demo source changed before live generation: expected 800 rows")
        self._save(CATCHING_UP, copying=info, source_count_at_copy=copy_count)

    def stage_catching_up(self) -> None:
        self._start_bot_batch("source", self.cc.min_live_rows_after_snapshot)
        wm = self.watermark

        # 1) 스냅샷 집합(기준점 이전)을 한 번 확정한다. 아직 sink 가 적용 중이면 통과할 때까지 기다린다.
        #    원본은 계속 쓰이는 중이지만 이 집합은 불변이라 트래픽을 멈추지 않고 확인할 수 있다.
        if not (self.job.checkpoint.get("snapshot_set") or {}).get("passed"):
            def snapshot_verified() -> tuple[bool, dict[str, Any]]:
                healthy, states = self._connectors_healthy()
                res = self._verify_snapshot_set("snapshot")
                return healthy and bool(res.get("passed")), {"connectors": states, **res}
            snap = self._wait("snapshot_verify", snapshot_verified)
            self._save(snapshot_set={"passed": True, "count": snap.get("source_count"), "skipped": bool(snap.get("skipped")),
                                     "report": snap.get("report"), "method": snap.get("method")})

        # 2) 기준점 이후(delta) 행이 최소 건수만큼 생기고 최신 행이 대상에 반영됐는지 본다. 전체 count 는 세지 않는다.
        need = self.cc.min_live_rows_after_snapshot

        def caught_up() -> tuple[bool, dict[str, Any]]:
            healthy, states = self._connectors_healthy()
            src_n = self.p.db.count_rows_in_scope(self.source, since=wm)
            tgt_n = self.p.db.count_rows_in_scope(self.target, since=wm)
            latest = self.p.db.latest_ids(self.source, 10, since=wm)
            applied = all(self.p.db.row_exists(self.target, rid) for rid in latest)
            ok = healthy and src_n >= need and applied
            return ok, {"connectors": states, "delta_source": src_n, "delta_target": tgt_n, "need": need,
                        "latest_applied": applied, "watermark": wm}

        info = self._wait("catching_up", caught_up)
        self._wait_bot_batch("source")
        if self.cc.demo_total_rows is not None:
            info["source"] = self.p.db.count_rows(self.source)
            if info["source"] != 900:
                raise CutoverError("demo source must contain exactly 900 rows")
        self._save(READY, catching_up=info)

    def stage_ready(self) -> None:
        if not self.approve:
            raise ApprovalRequired("READY: 쓰기 차단은 되돌리기 어려운 단계다. --approve <승인 ID> 로 재실행한다")
        self._save(FENCING, approval={"id": self.approve, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def stage_fencing(self) -> None:
        self._policy_guard("pre_cutover")
        if not self.job.checkpoint.get("fence_started_at"):
            self._save(fence_started_at=time.time())
        self._set_bot_generation(False)
        self._wait_bot_settled()
        ingress_paused: dict[str, Any] | None = None
        if self.ingress is not None:
            # 접수는 계속 받되(202) 앱으로 보내는 것만 멈춘다. settled 가 아니면 원본을 닫지 않는다.
            ingress_paused = self._step("ingress_pause", lambda: self.ingress.pause())  # type: ignore[union-attr]
            snap = self._ingress_snapshot()
            if snap.get("mode") != "paused" or snap.get("in_flight"):
                raise CutoverError(f"접수 서비스가 paused 가 아니거나 전달 중 요청이 있다: {snap}")
            if ingress_paused.get("settled") is False:
                raise CutoverError(f"접수 서비스 pause 가 settled 되지 않았다: {ingress_paused}")
        self._step("revoke_source_app_insert", lambda: (self.p.db.revoke_app_insert(self.source), {"revoked": True})[1])
        self._step("terminate_source_app_sessions", lambda: {"terminated": self.p.db.terminate_app_sessions(self.source)})
        sentinel = str(uuid.uuid5(uuid.NAMESPACE_URL, f"migration-fence-test/{self.job_id}"))
        rejected = self.p.db.test_insert_rejected(self.source, sentinel)
        if not rejected:
            raise CutoverError("원본 앱 계정의 시험 INSERT 가 거절되지 않았다. 차단 실패")
        latest = self.p.db.latest_ids(self.source, 1)
        self._save(DRAINING, fenced={"sentinel": sentinel, "rejected": True,
                                    "last_committed": {"request_id": latest[0] if latest else None},
                                    "ingress": ingress_paused})

    def stage_draining(self) -> None:
        last_id = ((self.job.checkpoint.get("fenced") or {}).get("last_committed") or {}).get("request_id")
        sink = self.config.job.sink_connector
        stable_since: dict[str, Any] = {"offsets": None, "at": None}
        offsets_supported = self.p.connect.offsets(sink) is not None
        if not offsets_supported:
            log.warning("Connect /offsets API 를 쓸 수 없다. Kafka offset H+1 비교 없이 DB 커밋 확인만으로 판정한다")

        wm = self.watermark
        snapshot_count = (self.job.checkpoint.get("snapshot_set") or {}).get("count")

        def drained() -> tuple[bool, dict[str, Any]]:
            healthy, states = self._connectors_healthy()
            # 원본은 fencing 됐으므로 delta(기준점 이후) 건수가 양쪽에서 같고 마지막 커밋 행이 대상에 있으면 적용이 끝난 것이다.
            # 기준점 이전 집합은 CATCHING_UP 에서 확정했지만, 늦게 커밋된 행이 섞일 수 있어 건수만 싸게 다시 센다.
            src_n = self.p.db.count_rows_in_scope(self.source, since=wm)
            tgt_n = self.p.db.count_rows_in_scope(self.target, since=wm)
            pre_src = self.p.db.count_rows_in_scope(self.source, upto=wm) if wm is not None else 0
            pre_tgt = self.p.db.count_rows_in_scope(self.target, upto=wm) if wm is not None else 0
            last_ok = last_id is None or self.p.db.row_exists(self.target, last_id)
            info: dict[str, Any] = {"connectors": states, "delta_source": src_n, "delta_target": tgt_n,
                                    "snapshot_source": pre_src, "snapshot_target": pre_tgt, "watermark": wm,
                                    "last_request_id": last_id, "last_in_target": last_ok,
                                    "offsets_supported": offsets_supported}
            stable = True
            if offsets_supported:
                cur = sink_kafka_offsets(self.p.connect.offsets(sink))
                info["sink_offsets"] = cur
                now = self.clock()
                if cur != stable_since["offsets"]:
                    stable_since.update(offsets=cur, at=now)
                stable = (now - float(stable_since["at"] or now)) >= self.cc.offset_stable_seconds
                info["offsets_stable_seconds"] = round(now - float(stable_since["at"] or now), 1)
            return healthy and last_ok and src_n == tgt_n and pre_src == pre_tgt and stable, info

        info = self._wait("cdc_drain", drained)
        # 스냅샷 집합에 CATCHING_UP 확정 뒤 늦게 커밋된 행이 들어왔으면(건수 변화) 그 집합만 digest 로 다시 확정한다.
        snapshot_recheck: dict[str, Any] = {"needed": False}
        if wm is not None and snapshot_count is not None and info["snapshot_source"] != snapshot_count:
            res = self._verify_snapshot_set("snapshot-recheck")
            snapshot_recheck = {"needed": True, "before": snapshot_count, "after": info["snapshot_source"], **res}
            if not res.get("passed"):
                raise CutoverError(f"기준점 이전 집합에 늦게 들어온 행이 대상과 맞지 않는다: {res}")
            self._save(snapshot_set={"passed": True, "count": info["snapshot_source"],
                                     "report": res.get("report"), "method": res.get("method")})
        source_offsets = self.p.connect.offsets(self.config.job.source_connector)
        self._save(SYNC_CONFIRMED, drained=info, source_offsets=source_offsets, snapshot_recheck=snapshot_recheck,
                   offset_note=None if offsets_supported else "Connect /offsets 미지원: DB 커밋 확인으로 대체")

    def stage_sync_confirmed(self) -> None:
        # 전환 구간에서는 행을 비교하지 않는다. DRAINING 이 적용 완료를 확인했으므로 건수만 다시 센다.
        first = self._count_check("pre-stop")
        if not first["passed"]:
            raise CutoverError(f"전환 전 건수 불일치: {first}")
        evidence: dict[str, Any] = {}
        for name in (self.config.job.source_connector, self.config.job.sink_connector):
            if self.p.connect.exists(name):
                evidence[name] = {"config": mask_config(self.p.connect.get_config(name)),
                                  "status": self.p.connect.status(name), "offsets": self.p.connect.offsets(name)}
        self._step("connector_evidence", lambda: evidence)
        self._step("delete_source_connector", lambda: {"deleted": self.p.connect.delete(self.config.job.source_connector)})
        self._step("delete_sink_connector", lambda: {"deleted": self.p.connect.delete(self.config.job.sink_connector)})
        if self.p.db.app_can_insert(self.target):
            raise CutoverError("커넥터 종료 후에도 대상 앱 쓰기가 닫혀 있어야 하는데 열려 있다")
        second = self._count_check("post-stop")
        if not second["passed"]:
            raise CutoverError(f"커넥터 종료 후 건수 불일치: {second}")
        self._save(SWITCHING, counts_pre_stop=first, counts_post_stop=second)

    def stage_switching(self) -> None:
        self._policy_guard("pre_cutover")
        if self.p.db.app_can_insert(self.source):
            raise CutoverError("원본 쓰기 차단이 해제되어 전환을 진행할 수 없다")
        # A resumed grant may already have allowed new target writes. Do not
        # compare against a frozen source or pretend it is safe to roll back.
        if self.store.step_done(self.job_id, "grant_target_app_insert") is None:
            if self.p.db.app_can_insert(self.target):
                raise CutoverError("대상 쓰기가 이미 열려 있어 정책 전환을 재개할 수 없다")
            if any(self.p.connect.exists(name) for name in (
                    self.config.job.source_connector, self.config.job.sink_connector)):
                raise CutoverError("최종 정책 판정 전 CDC 커넥터가 남아 있다")
            compared = self._count_check("policy-pre-grant")
            if not compared.get("passed"):
                raise CutoverError(f"정책 판정 직전 건수 불일치: {compared}")
            self._save(counts_pre_grant=compared)
            checks = {
                "final_sync": build_check("pass", {
                    "source_writes_fenced": not self.p.db.app_can_insert(self.source),
                    "target_applied_through_checkpoint": bool(compared.get("passed")),
                    "compare_passed": bool(compared.get("passed")),
                }, source="migration.cutover"),
                "write_wait": build_check("pass" if self.job.checkpoint.get("fence_started_at") else "unknown", {
                    "elapsed_seconds": max(0, time.time() - float(self.job.checkpoint.get("fence_started_at", time.time()))),
                }, source="migration.cutover"),
            }
            self._policy_guard("final_verify", checks)
        if not self.job.checkpoint.get("target_grant_attempted"):
            self._set_bot_generation(False)
            self._wait_bot_settled()
            snap = self._ingress_snapshot()
            if snap["mode"] != "paused" or snap["in_flight"] or snap["target"] != self._ingress_name(self.source):
                raise CutoverError("대상 권한을 열기 전 접수 큐가 원본에서 정지되어야 한다")
            self._save(target_rows_before=self.p.db.count_rows(self.target))
        if "target_rows_before" not in self.job.checkpoint:
            raise CutoverError("대상 쓰기 전 건수 기준점이 없다. 수동 복구 필요")
        self._save(target_grant_attempted=True)
        self._step("grant_target_app_insert", lambda: (self.p.db.grant_app_insert(self.target), {"granted": True})[1])
        ingress_switch = self._switch_ingress()
        traffic_switch = self._switch_traffic() if self.p.traffic is not None else None
        self._start_bot_batch("target", self.cc.min_rows_after_switch)
        self._save(VERIFYING, switched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   ingress=ingress_switch, traffic=traffic_switch)

    def _switch_traffic(self) -> dict[str, Any]:
        desired = self._route_name(self.target)

        def switch() -> dict[str, Any]:
            current = self.p.traffic.status().get("target")  # type: ignore[union-attr]
            if current == desired:
                return {"already": True, "target": current}
            res = self.p.traffic.switch(desired)  # type: ignore[union-attr]
            return {"already": False, "from": current, "target": res.get("target"), "changed_at": res.get("changed_at")}

        switched = self._step("public_traffic_target", switch)
        current = self.p.traffic.status().get("target")  # type: ignore[union-attr]
        if current != desired:
            raise CutoverError(f"공개 도메인 경로가 {desired} 가 아니다: {current}")
        return switched

    def _switch_ingress(self) -> dict[str, Any]:
        """대상 앱이 준비됐는지 확인한 뒤 접수 큐를 대상으로 돌리고 재개한다. 대상 GRANT 이후에만 호출된다."""
        desired = self._ingress_name(self.target)

        def app_ready() -> tuple[bool, dict[str, Any]]:
            res = self.ingress.check_app(self.target)  # type: ignore[union-attr]
            return bool(res.get("ok")) and not res.get("skipped"), res

        # 대상 앱의 쓰기 열기(GitOps) 가 끝나기를 기다린다. 읽기만 하므로 _step 이 아니다.
        ready = self._wait("target_app_ready", app_ready)

        def set_target() -> dict[str, Any]:
            snap = self._ingress_snapshot()
            if snap.get("target") == desired:
                return {"already": True, **snap}
            if snap.get("mode") != "paused" or snap.get("in_flight"):
                raise CutoverError(f"접수 서비스가 paused 가 아니라 target 을 바꿀 수 없다: {snap}")
            res = self.ingress.set_target(desired, str(snap.get("epoch")))  # type: ignore[union-attr]
            return {"already": False, "from": snap.get("target"), "target": res.get("target"), "epoch": res.get("epoch")}

        targeted = self._step("ingress_target", set_target)
        snap = self._ingress_snapshot()
        if snap.get("target") != desired:
            raise CutoverError(f"접수 서비스 target 이 {desired} 가 아니다: {snap}")

        def resume() -> dict[str, Any]:
            cur = self._ingress_snapshot()
            if cur.get("mode") == "running":
                return {"already": True, **cur}
            res = self.ingress.resume(str(cur.get("epoch")))  # type: ignore[union-attr]
            return {"already": False, "mode": res.get("mode"), "epoch": res.get("epoch")}

        # A queue process restarts paused. Reconcile live state even if an earlier
        # resume response was checkpointed before the runner crashed.
        resumed = resume()
        self.store.mark_step(self.job_id, "ingress_resume", {"result": resumed})
        snap = self._ingress_snapshot()
        if snap["mode"] != "running" or snap["target"] != desired:
            raise CutoverError("접수 큐 대상/재개 확인 실패")
        return {"app_ready": ready, "target": targeted, "resume": resumed}

    def stage_verifying(self) -> None:
        # Resume only the remaining requests of the saved batch after a transient failure.
        if "bot_target_limit" not in self.job.checkpoint:
            raise CutoverError("대상 트래픽봇 한도 기록이 없다. 수동 복구 필요")
        snap = self._ingress_snapshot()
        if snap["target"] != self._ingress_name(self.target):
            raise CutoverError("검증 재개 전 접수 큐 대상이 바뀌었다. 수동 복구 필요")
        self._switch_ingress()
        self._start_bot_batch("target", self.cc.min_rows_after_switch)
        bs = self._wait_bot_batch("target")
        self._set_bot_generation(False)
        base = self.job.checkpoint.get("target_rows_before")
        if base is None:
            raise CutoverError("대상 쓰기 전 건수 기준점이 없다. 수동 복구 필요")
        target_rows = self.p.db.count_rows(self.target)
        info = {"bot": bs, "target_rows_before": base, "target_rows": target_rows}
        if target_rows < base + self.cc.min_rows_after_switch:
            raise CutoverError(f"대상 신규 쓰기 부족: {info}")
        if self.cc.demo_total_rows is not None:
            source_rows = self.p.db.count_rows(self.source)
            if source_rows != 900 or target_rows != 1000:
                raise CutoverError(f"demo row budget mismatch: source={source_rows}, target={target_rows}")
            info["demo_rows"] = {"source": source_rows, "target": target_rows}
        if self.p.db.app_can_insert(self.source):
            raise CutoverError("원본 앱 쓰기가 다시 열려 있다")
        if self.ingress is not None:
            snap = self._ingress_snapshot()
            if snap.get("mode") != "running" or snap.get("target") != self._ingress_name(self.target):
                raise CutoverError(f"접수 서비스가 대상으로 돌아가고 있지 않다: {snap}")
            info["ingress"] = snap
        if self.p.traffic is not None:
            route = self.p.traffic.status().get("target")
            if route != self._route_name(self.target):
                raise CutoverError(f"공개 도메인이 대상으로 가고 있지 않다: {route}")
            info["traffic"] = {"target": route}
        # 전환 후: 건수만 확인한다. 대상은 이미 쓰기 중이라 기준점 이후 건수는 원본 이상이면 되고, 기준점 이전 건수는 같아야 한다.
        # 행 단위 비교가 필요하면 전환이 끝난 뒤 `python -m migration.verify --mode post --since <watermark>` 로 따로 돌린다.
        result = self._count_check("final", allow_target_extra=True)
        if not result["passed"]:
            raise CutoverError(f"전환 후 건수 불일치: {result}")
        cleanup: dict[str, Any] = {"skipped": True}
        if self.cc.drop_source_slot_on_complete:
            def _cleanup() -> dict[str, Any]:
                try:
                    return self.p.db.drop_source_slot(self.source)
                except Exception:  # noqa: BLE001 - 정리 실패는 전환 결과를 바꾸지 않는다. 경고만 남기고 수동 정리로 넘긴다
                    log.warning("원본 후처리 실패 — 같은 설정으로 python -m migration.source_cleanup 재실행 필요")
                    return {"error": "SOURCE_CLEANUP_FAILED"}
            cleanup = self._step("drop_source_slot", _cleanup)
            if cleanup.get("error"):
                log.warning("원본 후처리 미완료 (%s) — python -m migration.source_cleanup --config <same-config>",
                            cleanup["error"])
        self._save(COMPLETED, counts_final=result, post_switch=info, source_cleanup=cleanup)

    # ---- 실행 -----

    def plan(self) -> list[tuple[str, str]]:
        return [
            (PRECHECK, "양쪽 DB·트래픽봇 제어/원장·접수 큐 경로·Connect 확인"),
            (COPYING, "기준점(원본 max(created_at)) 저장, publication/slot 준비, Source/Sink 등록, snapshot(seed) 완료 대기"),
            (CATCHING_UP, f"기준점 이전 집합 digest 확정 → 기준점 이후 최소 {self.cc.min_live_rows_after_snapshot}건 반영 확인"),
            (READY, "승인 대기 (--approve)"),
            (FENCING, "트래픽봇 생성 정지 → 미확인 요청 처리 완료" + (" → 접수 큐 pause(settled)" if self.config.ingress.enabled else "")
             + " → 원본 REVOKE INSERT + 세션 종료 → 시험 INSERT 거절 확인"),
            (DRAINING, "마지막 커밋 요청의 대상 반영·기준점 이후 건수 일치·sink offset 정체 확인"),
            (SYNC_CONFIRMED, "기준점 이후·이전 건수 확인 → 커넥터 증거 보관·삭제 → 건수 재확인"),
            (SWITCHING, "대상 GRANT INSERT"
             + (" → 대상 앱 readiness → 접수 큐 /target → /resume" if self.config.ingress.enabled else "")
             + (" → 공개 도메인 경로 전환" if self.config.traffic.enabled else "")
             + "  [여기부터 자동 원복 없음]"),
            (VERIFYING, f"대상 신규 {self.cc.min_rows_after_switch}건 확인 → 건수 확인(대상 delta ≥ 원본, 스냅샷 건수 일치)"),
            (COMPLETED, "트래픽봇 생성 정지·미확인 요청 0 확인 후 완료"),
        ]

    def run(self, *, retry: bool = False, force_retry: bool = False) -> str:
        stage = self.job.stage
        if stage not in (COMPLETED, ROLLED_BACK) and self.job.checkpoint.get("writer_mode") != "traffic_bot_v1":
            raise CutoverError("옛 DB 워커 작업은 자동 재개하지 않는다. 상태 확인 후 새 job_id 로 시작해야 한다")
        if stage in (ABORTED, NEEDS_RECOVERY):
            failed = str(self.job.checkpoint.get("failed_stage") or PRECHECK)
            if stage == NEEDS_RECOVERY and not force_retry:
                raise CutoverError("NEEDS_RECOVERY: 대상에 앱 쓰기를 연 뒤 실패했다. 사람이 authoritative DB 를 확정한 뒤 "
                                   "--force-retry 로만 재개한다")
            if stage == ABORTED and not retry:
                raise CutoverError(f"ABORTED (실패 단계 {failed}). 원인을 확인한 뒤 --retry 로 재개하거나 abort 명령으로 원복한다")
            log.warning("%s 상태에서 %s 단계부터 재개", stage, failed)
            stage = failed
            self._save(stage, resumed_from=self.job.stage)
        if stage in TERMINAL:
            log.info("작업 %s 는 이미 %s", self.job_id, stage)
            return stage
        handlers = {
            PRECHECK: self.stage_precheck, COPYING: self.stage_copying, CATCHING_UP: self.stage_catching_up,
            READY: self.stage_ready, FENCING: self.stage_fencing, DRAINING: self.stage_draining,
            SYNC_CONFIRMED: self.stage_sync_confirmed, SWITCHING: self.stage_switching, VERIFYING: self.stage_verifying,
        }
        idx = STAGES.index(stage)
        for current in STAGES[idx:]:
            if current == COMPLETED:
                break
            log.info("=== 단계 %s 시작 (job=%s %s) ===", current, self.job_id, self.job.direction)
            try:
                handlers[current]()
            except ApprovalRequired:
                raise
            except Exception as exc:  # noqa: BLE001 - 어떤 실패든 상태를 남긴다
                failure = NEEDS_RECOVERY if self._target_writes_possible(current) else ABORTED
                stop_error = None
                if self.job.checkpoint.get("bot_identity"):
                    try:
                        self._set_bot_generation(False)
                    except Exception:
                        stop_error = "BOT_STOP_FAILED"
                self._save(failure, failed_stage=current, error=str(exc)[:1000], bot_stop_error=stop_error)
                if current == SWITCHING and failure == ABORTED:
                    try:
                        self.abort_rollback()
                        failure = ROLLED_BACK
                    except Exception as recovery_error:
                        self._save(NEEDS_RECOVERY, recovery_error=str(recovery_error)[:1000])
                        failure = NEEDS_RECOVERY
                log.error("단계 %s 실패 → %s: %s", current, failure, exc)
                raise CutoverError(f"{current} 실패 → {failure}: {exc}") from exc
            log.info("=== 단계 %s 완료 → %s ===", current, self.job.stage)
        return self.job.stage

    def _target_writes_possible(self, stage: str) -> bool:
        # Persisted intent precedes GRANT. A missing response is not proof that
        # the grant failed; never reopen the source in that case.
        if self.job.checkpoint.get("target_grant_attempted"):
            return True
        if self.store.step_done(self.job_id, "grant_target_app_insert") is not None:
            return True
        if STAGES.index(stage) >= STAGES.index(VERIFYING):
            return True
        if stage == SWITCHING and self.job.checkpoint.get("grant_tracking_version") != 1:
            return True  # legacy jobs have no durable grant intent
        return False

    def abort_rollback(self) -> dict[str, Any]:
        """ABORTED 이고 대상 쓰기 전이면: 커넥터 삭제, 원본 앱 INSERT 복구, 접수 큐(원본) 재개, 트래픽봇은 정지 유지."""
        if self.job.stage != ABORTED:
            raise CutoverError(f"abort 는 ABORTED 상태에서만 가능 (현재 {self.job.stage})")
        failed = str(self.job.checkpoint.get("failed_stage") or PRECHECK)
        if self._target_writes_possible(failed):
            raise CutoverError("대상 쓰기 이후 실패는 자동 원복하지 않는다")
        if self.p.db.app_can_insert(self.target):
            raise CutoverError("대상 앱 쓰기가 열려 있다. 자동 원복 불가")
        self._set_bot_generation(False)
        if self.ingress is not None:
            snap = self._ingress_snapshot()
            if snap.get("target") != self._ingress_name(self.source):
                raise CutoverError("접수 서비스 target 이 원본이 아니다. 수동 확인 필요")
        out: dict[str, Any] = {}
        for name in (self.config.job.source_connector, self.config.job.sink_connector):
            out[name] = {"deleted": self.p.connect.delete(name)}
        self.p.db.grant_app_insert(self.source)
        out["source_app_insert"] = self.p.db.app_can_insert(self.source)
        if not out["source_app_insert"]:
            raise CutoverError("원본 쓰기 복구 확인 실패")
        out["bot"] = "stopped"
        if self.ingress is not None:
            snap = self._ingress_snapshot()
            if snap.get("target") != self._ingress_name(self.source):
                raise CutoverError(f"접수 서비스 target 이 원본이 아니다: {snap}. 수동 확인 필요")
            if snap.get("mode") != "running":
                res = self.ingress.resume(str(snap.get("epoch")))
                snap = {**snap, "mode": res.get("mode")}
            out["ingress"] = snap
        self._save(ROLLED_BACK, rollback=out)
        return out


# ---- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="온프렘→AWS 전환 상태 머신")
    p.add_argument("--config", default=os.environ.get("MIGRATION_CONFIG", "config.yaml"))
    p.add_argument("--state-dir", default=os.environ.get("WORKER_STATE_DIR"))
    p.add_argument("--report-dir", default=None, help="verify 보고서 저장 위치 (기본: state-dir/reports)")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    subs = p.add_subparsers(dest="command", required=True)
    sp = subs.add_parser("run", help="저장된 단계부터 전환 실행")
    sp.add_argument("--approve", default=None, help="READY 이후 진행 승인 ID")
    sp.add_argument("--retry", action="store_true", help="ABORTED 작업을 실패 단계부터 재개")
    sp.add_argument("--force-retry", action="store_true", help="NEEDS_RECOVERY 작업을 사람이 확인한 뒤 재개")
    sp.add_argument("--dry-run", action="store_true", help="외부 호출 없이 현재 상태와 남은 단계만 출력")
    subs.add_parser("status", help="저장된 상태·체크포인트 출력")
    subs.add_parser("abort", help="ABORTED(대상 쓰기 전) 작업을 원본 유일 writer 로 원복")
    return p


def _build(config: Config, state_dir: Path, report_dir: Path, approve: str | None) -> Cutover:
    if not config.bot.control_url or not config.bot.request_endpoint or not config.ingress.enabled:
        raise ConfigError("러너에는 bot.control_url, bot.request_endpoint, ingress.control_url 이 필요하다")
    for target in (config.job.source, config.job.target):
        config.ingress.ingress_target(target)
        check = config.ingress.app_checks.get(target)
        if check is None or not check.environment or check.database != config.target(target).dbname:
            raise ConfigError(f"ingress.app_checks.{target}: 실제 environment/database readiness 확인 필요")
    store = CutoverStore(state_dir)
    docs = load_connector_template_pair("forward", template_variables(config))
    ports = Ports(
        bot=HttpBotClient(config.bot),
        connect=ConnectClient(config.kafka.connect_url),
        db=PgAdminPort(config),
        verify=VerifyAdapter(config, state_dir, report_dir),
        ingress=IngressAdapter(config) if config.ingress.enabled else None,
        traffic=HttpTrafficClient(config.traffic) if config.traffic.enabled else None,
    )
    return Cutover(config, store, ports, connector_docs=docs, approve=approve,
                   policy_gate=CutoverPolicyGate(config, state_dir))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cut = None
    try:
        config = load_config(args.config)
        state_dir = Path(args.state_dir or config.cutover.state_dir)
        report_dir = Path(args.report_dir) if args.report_dir else state_dir / "reports"
        if args.command == "status":
            job = CutoverStore(state_dir).load(config.job.job_id)
            print(json.dumps(job.__dict__ if job else {"job_id": config.job.job_id, "stage": None},
                             ensure_ascii=False, indent=2, default=str))
            return EXIT_OK
        if args.command == "run" and args.dry_run:
            store = CutoverStore(state_dir)
            job = store.load(config.job.job_id)
            stage = job.stage if job else PRECHECK
            print(f"[dry-run] job={config.job.job_id} {config.job.source}->{config.job.target} 현재 단계={stage}")
            cut = Cutover(config, store, Ports(bot=None, connect=None, db=None, verify=None),  # type: ignore[arg-type]
                          connector_docs={}, approve=args.approve)
            start = STAGES.index(stage) if stage in STAGES else 0
            for name, desc in cut.plan()[start:]:
                print(f"  {name:<15} {desc}")
            if stage in (ABORTED, NEEDS_RECOVERY):
                print(f"  (현재 {stage}: 실패 단계 {job.checkpoint.get('failed_stage') if job else None})")
            return EXIT_OK
        cut = _build(config, state_dir, report_dir, getattr(args, "approve", None))
        if args.command == "abort":
            print(json.dumps(cut.abort_rollback(), ensure_ascii=False, indent=2, default=str))
            return EXIT_OK
        final = cut.run(retry=args.retry, force_retry=args.force_retry)
        print(json.dumps({"job_id": config.job.job_id, "stage": final}, ensure_ascii=False))
        return EXIT_OK if final == COMPLETED else EXIT_FAILED
    except ApprovalRequired as exc:
        log.warning("%s", exc)
        return EXIT_WAITING_APPROVAL
    except (ConfigError,) as exc:
        log.error("설정 오류: %s", exc)
        return EXIT_CONFIG
    except (CutoverError, ConnectError, BotClientError, IngressClientError, psycopg.Error) as exc:
        log.error("전환 실패: %s", exc)
        return EXIT_FAILED
    finally:
        if cut is not None and isinstance(cut.p.db, PgAdminPort):
            cut.p.db.close()


if __name__ == "__main__":
    sys.exit(main())
