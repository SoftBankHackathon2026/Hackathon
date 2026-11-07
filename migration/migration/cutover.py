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
    drop_slot, sink_kafka_offsets, template_variables
from .config import Config, ConfigError, load_config
from .state import CutoverJob, CutoverStore, RequestStore
from .cutover_policy import CutoverPolicyGate
from .policy_client import build_check
from .worker_client import HttpWorkerClient, WorkerClientError

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


class WorkerPort(Protocol):
    def status(self) -> dict[str, Any]: ...
    def pause(self) -> dict[str, Any]: ...
    def resume(self) -> dict[str, Any]: ...
    def drain(self, timeout: float = 60.0) -> dict[str, Any]: ...
    def switch_target(self, target: str, phase: str | None = None) -> dict[str, Any]: ...


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
    def row_exists(self, target: str, request_id: str) -> bool: ...
    def latest_ids(self, target: str, n: int) -> list[str]: ...
    def prepare_source(self, target: str) -> dict[str, Any]: ...
    def drop_source_slot(self, target: str) -> dict[str, Any]: ...


class VerifyPort(Protocol):
    def run(self, mode: str, label: str) -> dict[str, Any]: ...


# ---- 실제 어댑터 -----------------------------------------------------------------


class PgAdminPort:
    """psycopg 로 관리자 작업을 수행한다. 앱 계정 차단·시험 INSERT 도 여기서."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.table = config.job.table

    def _admin(self, target: str) -> psycopg.Connection[Any]:
        return dbmod.connect(self.config.connection(target, "admin", application_name="migration-cutover"))

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

    def latest_ids(self, target: str, n: int) -> list[str]:
        with self._admin(target) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT request_id FROM {} ORDER BY created_at DESC, request_no DESC LIMIT %s")
                            .format(dbmod.table_ident(self.table)), (n,))
                return [str(r[0]) for r in cur.fetchall()]

    def prepare_source(self, target: str) -> dict[str, Any]:
        job = self.config.job
        with self._admin(target) as conn:
            return prepare_source(conn, publication=job.publication_name, slot=job.slot_name, table=job.table,
                                  owner=self.config.target(target).user_spec("cdc").user)

    def drop_source_slot(self, target: str) -> dict[str, Any]:
        """커넥터 삭제 뒤 원본에 남은 비활성 slot 과 publication 을 지운다. 남겨 두면 WAL 이 계속 쌓인다."""
        job = self.config.job
        with self._admin(target) as conn:
            slot_dropped = drop_slot(conn, job.slot_name)
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_publication WHERE pubname = %s", (job.publication_name,))
                pub_exists = cur.fetchone() is not None
                if pub_exists:
                    cur.execute(sql.SQL("DROP PUBLICATION {}").format(sql.Identifier(job.publication_name)))
            conn.commit()
        return {"slot": job.slot_name, "slot_dropped": slot_dropped,
                "publication": job.publication_name, "publication_dropped": pub_exists}


class VerifyAdapter:
    def __init__(self, config: Config, state_dir: Path, report_dir: Path) -> None:
        self.config, self.state_dir, self.report_dir = config, state_dir, report_dir

    def run(self, mode: str, label: str) -> dict[str, Any]:
        path = self.report_dir / f"verify-{self.config.job.job_id}-{label}.json"
        cmp = verifymod.run(self.config, source=self.config.job.source, target=self.config.job.target,
                            state_dir=self.state_dir, mode=mode, report_path=path, extra_meta={"label": label})
        return {"passed": cmp.passed, "counts": cmp.counts, "report": str(path),
                "source_count": cmp.source_count, "target_count": cmp.target_count}


# ---- 상태 머신 --------------------------------------------------------------------


@dataclass
class Ports:
    worker: WorkerPort
    connect: ConnectPort
    db: DbPort
    verify: VerifyPort
    request_store: RequestStore | None = None


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
            target=self.target, stage=PRECHECK, checkpoint={}, updated_at="")
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
        ws = self.p.worker.status()
        if ws.get("target") != self.source:
            problems.append(f"워커 target={ws.get('target')} 이 원본 {self.source} 가 아니다")
        known = ws.get("known_targets")
        if known is not None:  # 구버전 워커는 이 키가 없다
            for name in (self.source, self.target):
                if name not in known:
                    problems.append(f"워커 설정에 target {name} 이 없다 (known_targets={list(known)}). "
                                    "워커 ConfigMap 에 추가하지 않으면 SWITCHING 의 /target 전환이 실패한다")
        connectors = self.p.connect.list_connectors()
        if problems:
            raise CutoverError("PRECHECK 실패: " + "; ".join(problems))
        self._save(COPYING, precheck={"source": src, "target": tgt, "worker": {k: ws.get(k) for k in ("state", "target", "phase", "counters")},
                                     "connectors": connectors})

    def stage_copying(self) -> None:
        self._policy_guard("pre_start")
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
            return healthy and seed_tgt >= seed_src and seed_src > 0, {"connectors": states, "seed_source": seed_src, "seed_target": seed_tgt}

        info = self._wait("snapshot", snapshot_done)
        copy_count = self.p.db.count_rows(self.source)
        self._save(CATCHING_UP, copying=info, source_count_at_copy=copy_count)

    def stage_catching_up(self) -> None:
        base = int(self.job.checkpoint.get("source_count_at_copy", 0))
        need = base + self.cc.min_live_rows_after_snapshot

        def caught_up() -> tuple[bool, dict[str, Any]]:
            healthy, states = self._connectors_healthy()
            src_n = self.p.db.count_rows(self.source)
            tgt_n = self.p.db.count_rows(self.target)
            latest = self.p.db.latest_ids(self.source, 10)
            applied = all(self.p.db.row_exists(self.target, rid) for rid in latest)
            ok = healthy and src_n >= need and applied
            return ok, {"connectors": states, "source": src_n, "target": tgt_n, "need": need, "latest_applied": applied}

        info = self._wait("catching_up", caught_up)
        self._save(READY, catching_up=info)

    def stage_ready(self) -> None:
        if not self.approve:
            raise ApprovalRequired("READY: 쓰기 차단은 되돌리기 어려운 단계다. --approve <승인 ID> 로 재실행한다")
        self._save(FENCING, approval={"id": self.approve, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def stage_fencing(self) -> None:
        self._policy_guard("pre_cutover")
        if not self.job.checkpoint.get("fence_started_at"):
            self._save(fence_started_at=time.time())
        self._step("worker_pause", lambda: {"state": self.p.worker.pause().get("state")})
        drain = self._step("worker_drain", lambda: self.p.worker.drain(timeout=120))
        if not drain.get("settled"):
            raise CutoverError(f"워커 진행 중 요청이 정리되지 않았다: {drain}")
        self._step("revoke_source_app_insert", lambda: (self.p.db.revoke_app_insert(self.source), {"revoked": True})[1])
        self._step("terminate_source_app_sessions", lambda: {"terminated": self.p.db.terminate_app_sessions(self.source)})
        sentinel = str(uuid.uuid5(uuid.NAMESPACE_URL, f"migration-fence-test/{self.job_id}"))
        rejected = self.p.db.test_insert_rejected(self.source, sentinel)
        if not rejected:
            raise CutoverError("원본 앱 계정의 시험 INSERT 가 거절되지 않았다. 차단 실패")
        ws = self.p.worker.status()
        if ws.get("state") != "PAUSED" or ws.get("in_flight"):
            raise CutoverError(f"워커가 PAUSED 가 아니거나 진행 중 요청이 있다: {ws.get('state')} {ws.get('in_flight')}")
        last = drain.get("last_committed") or {}
        self._save(DRAINING, fenced={"sentinel": sentinel, "rejected": True, "last_committed": last,
                                    "queue_length": ws.get("queue_length")})

    def stage_draining(self) -> None:
        last_id = ((self.job.checkpoint.get("fenced") or {}).get("last_committed") or {}).get("request_id")
        if last_id is None and self.p.request_store is not None:
            rec = self.p.request_store.last_committed(self.source)
            last_id = rec.request_id if rec else None
        sink = self.config.job.sink_connector
        stable_since: dict[str, Any] = {"offsets": None, "at": None}
        offsets_supported = self.p.connect.offsets(sink) is not None
        if not offsets_supported:
            log.warning("Connect /offsets API 를 쓸 수 없다. Kafka offset H+1 비교 없이 DB 커밋 확인만으로 판정한다")

        def drained() -> tuple[bool, dict[str, Any]]:
            healthy, states = self._connectors_healthy()
            src_n, tgt_n = self.p.db.count_rows(self.source), self.p.db.count_rows(self.target)
            last_ok = last_id is None or self.p.db.row_exists(self.target, last_id)
            info: dict[str, Any] = {"connectors": states, "source": src_n, "target": tgt_n, "last_request_id": last_id,
                                    "last_in_target": last_ok, "offsets_supported": offsets_supported}
            stable = True
            if offsets_supported:
                cur = sink_kafka_offsets(self.p.connect.offsets(sink))
                info["sink_offsets"] = cur
                now = self.clock()
                if cur != stable_since["offsets"]:
                    stable_since.update(offsets=cur, at=now)
                stable = (now - float(stable_since["at"] or now)) >= self.cc.offset_stable_seconds
                info["offsets_stable_seconds"] = round(now - float(stable_since["at"] or now), 1)
            return healthy and last_ok and src_n == tgt_n and stable, info

        info = self._wait("cdc_drain", drained)
        source_offsets = self.p.connect.offsets(self.config.job.source_connector)
        self._save(SYNC_CONFIRMED, drained=info, source_offsets=source_offsets,
                   offset_note=None if offsets_supported else "Connect /offsets 미지원: DB 커밋 확인으로 대체")

    def stage_sync_confirmed(self) -> None:
        first = self.p.verify.run("pre", "pre-stop")
        if not first["passed"]:
            raise CutoverError(f"전환 전 검증 실패: {first}")
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
        second = self.p.verify.run("pre", "post-stop")
        if not second["passed"]:
            raise CutoverError(f"커넥터 종료 후 재검증 실패: {second}")
        self._save(SWITCHING, verify_pre=first, verify_post_stop=second)

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
            compared = self.p.verify.run("pre", "policy-pre-grant")
            if not compared.get("passed"):
                raise CutoverError("정책 판정 직전 데이터 비교 실패")
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
        self._step("grant_target_app_insert", lambda: (self.p.db.grant_app_insert(self.target), {"granted": True})[1])
        phase_after = self.config.job.live_phase_after

        def switch() -> dict[str, Any]:
            ws = self.p.worker.status()
            if ws.get("target") == self.target:
                return {"already": True, "target": ws.get("target")}
            res = self.p.worker.switch_target(self.target, phase_after)
            return {"target": res.get("target"), "phase": res.get("phase"), "last_switch": res.get("last_switch")}

        switched = self._step("worker_switch_target", switch)
        ws = self.p.worker.status()
        if ws.get("target") != self.target:
            raise CutoverError(f"워커 target 이 {self.target} 가 아니다: {ws.get('target')}")
        expected_db = self.config.target(self.target).dbname
        ident = ((ws.get("last_switch") or {}).get("identity") or {})
        if ident and ident.get("database") not in (None, expected_db):
            raise CutoverError(f"워커가 붙은 DB 가 기대와 다르다: {ident}")
        self._step("worker_resume", lambda: {"state": self.p.worker.resume().get("state")})
        before = int((ws.get("committed_by_target") or {}).get(self.target, 0))
        self._save(VERIFYING, switched=switched, committed_on_target_before=before,
                   switched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def stage_verifying(self) -> None:
        base = int(self.job.checkpoint.get("committed_on_target_before", 0))
        need = base + self.cc.min_rows_after_switch

        def enough() -> tuple[bool, dict[str, Any]]:
            ws = self.p.worker.status()
            n = int((ws.get("committed_by_target") or {}).get(self.target, 0))
            if ws.get("state") == "ERROR":
                raise CutoverError(f"워커 ERROR: {ws.get('last_error')}")
            return n >= need, {"committed_on_target": n, "need": need, "state": ws.get("state"),
                               "mismatch": (ws.get("counters") or {}).get("mismatch")}

        info = self._wait("post_switch_rows", enough)
        if int(info.get("mismatch") or 0) > 0:
            raise CutoverError(f"워커가 불변 필드 불일치를 기록했다: {info}")
        if self.p.db.app_can_insert(self.source):
            raise CutoverError("원본 앱 쓰기가 다시 열려 있다")
        result = self.p.verify.run("post", "final")
        if not result["passed"]:
            raise CutoverError(f"전환 후 검증 실패: {result}")
        cleanup: dict[str, Any] = {"skipped": True}
        if self.cc.drop_source_slot_on_complete:
            def _cleanup() -> dict[str, Any]:
                try:
                    return self.p.db.drop_source_slot(self.source)
                except Exception as exc:  # noqa: BLE001 - 정리 실패는 전환 결과를 바꾸지 않는다. 경고만 남기고 수동 정리로 넘긴다
                    log.warning("원본 slot/publication 정리 실패 — 수동으로 `python -m migration.cdc drop-slot` 실행: %s", exc)
                    return {"error": str(exc)}
            cleanup = self._step("drop_source_slot", _cleanup)
        self._save(COMPLETED, verify_final=result, post_switch=info, source_cleanup=cleanup)

    # ---- 실행 -----

    def plan(self) -> list[tuple[str, str]]:
        return [
            (PRECHECK, "양쪽 접속·스키마·권한·wal_level·워커 target·Connect 접근 확인"),
            (COPYING, "publication/slot 준비, Source/Sink 등록, snapshot(seed) 완료 대기"),
            (CATCHING_UP, f"snapshot 후 최소 {self.cc.min_live_rows_after_snapshot}건 추가 반영 확인"),
            (READY, "승인 대기 (--approve)"),
            (FENCING, "워커 pause → drain → 원본 REVOKE INSERT + 세션 종료 → 시험 INSERT 거절 확인"),
            (DRAINING, "마지막 커밋 요청의 대상 반영·건수 일치·sink offset 정체 확인"),
            (SYNC_CONFIRMED, "전체 비교(pre) → 커넥터 증거 보관·삭제 → 재검증"),
            (SWITCHING, "대상 GRANT INSERT → 워커 /target → /resume  [여기부터 자동 원복 없음]"),
            (VERIFYING, f"대상 신규 {self.cc.min_rows_after_switch}건 확인 → 전체 비교(post)"),
            (COMPLETED, "완료"),
        ]

    def run(self, *, retry: bool = False, force_retry: bool = False) -> str:
        stage = self.job.stage
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
                failure = NEEDS_RECOVERY if STAGES.index(current) >= STAGES.index(POINT_OF_NO_RETURN) else ABORTED
                self._save(failure, failed_stage=current, error=str(exc)[:1000])
                log.error("단계 %s 실패 → %s: %s", current, failure, exc)
                raise CutoverError(f"{current} 실패 → {failure}: {exc}") from exc
            log.info("=== 단계 %s 완료 → %s ===", current, self.job.stage)
        return self.job.stage

    def abort_rollback(self) -> dict[str, Any]:
        """ABORTED 이고 대상 쓰기 전이면: 커넥터 삭제, 원본 앱 INSERT 복구, 워커(원본) 재개."""
        if self.job.stage != ABORTED:
            raise CutoverError(f"abort 는 ABORTED 상태에서만 가능 (현재 {self.job.stage})")
        failed = str(self.job.checkpoint.get("failed_stage") or PRECHECK)
        if STAGES.index(failed) >= STAGES.index(POINT_OF_NO_RETURN):
            raise CutoverError("대상 쓰기 이후 실패는 자동 원복하지 않는다")
        if self.p.db.app_can_insert(self.target):
            raise CutoverError("대상 앱 쓰기가 열려 있다. 자동 원복 불가")
        out: dict[str, Any] = {}
        for name in (self.config.job.source_connector, self.config.job.sink_connector):
            out[name] = {"deleted": self.p.connect.delete(name)}
        self.p.db.grant_app_insert(self.source)
        out["source_app_insert"] = self.p.db.app_can_insert(self.source)
        ws = self.p.worker.status()
        if ws.get("target") != self.source:
            raise CutoverError(f"워커 target 이 원본이 아니다: {ws.get('target')}. 수동 확인 필요")
        out["worker"] = self.p.worker.resume().get("state")
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
    store = CutoverStore(state_dir)
    docs = load_connector_template_pair("forward", template_variables(config))
    ports = Ports(
        worker=HttpWorkerClient(config.worker.url),
        connect=ConnectClient(config.kafka.connect_url),
        db=PgAdminPort(config),
        verify=VerifyAdapter(config, state_dir, report_dir),
        request_store=RequestStore(state_dir),
    )
    return Cutover(config, store, ports, connector_docs=docs, approve=approve,
                   policy_gate=CutoverPolicyGate(config, state_dir))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
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
            cut = Cutover(config, store, Ports(worker=None, connect=None, db=None, verify=None),  # type: ignore[arg-type]
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
    except (CutoverError, ConnectError, WorkerClientError, psycopg.Error) as exc:
        log.error("전환 실패: %s", exc)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
