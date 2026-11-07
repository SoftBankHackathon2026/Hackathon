"""cutover.py: 가짜 포트로 상태 전이·재개·멱등성·실패 분기 검증."""

from __future__ import annotations

from typing import Any

import pytest

from migration.cutover import (ABORTED, CATCHING_UP, COMPLETED, COPYING, DRAINING, FENCING, NEEDS_RECOVERY, PRECHECK, READY,
                               ROLLED_BACK, SWITCHING, VERIFYING, ApprovalRequired, Cutover, CutoverError, Ports)
from migration.state import CutoverStore


class FakeWorker:
    def __init__(self, source: str) -> None:
        self.state = "RUNNING"
        self.target = source
        self.phase = "onprem_live"
        self.committed = {source: 1010}
        self.calls: list[str] = []
        self.last_switch: dict[str, Any] | None = None
        self.known_targets = ["onprem", "aws"]

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "target": self.target, "phase": self.phase, "in_flight": None,
                "known_targets": list(self.known_targets),
                "queue_length": 2, "counters": {"mismatch": 0}, "committed_by_target": dict(self.committed),
                "last_switch": self.last_switch, "last_error": None}

    def pause(self):
        self.calls.append("pause"); self.state = "PAUSED"; return self.status()

    def resume(self):
        self.calls.append("resume"); self.state = "RUNNING"; return self.status()

    def drain(self, timeout: float = 60.0):
        self.calls.append("drain")
        return {"settled": True, "queue_length": 2, "last_committed": {"request_id": "last-1", "target": self.target}}

    def switch_target(self, target: str, phase: str | None = None):
        self.calls.append(f"switch:{target}")
        assert self.state == "PAUSED"
        self.target = target
        self.phase = phase or self.phase
        self.last_switch = {"identity": {"database": "migration_demo"}}
        self.committed.setdefault(target, 0)
        return self.status()


class FakeConnect:
    def __init__(self) -> None:
        self.connectors: dict[str, dict[str, Any]] = {}
        self.puts: list[str] = []
        self.deletes: list[str] = []
        self.offsets_supported = True
        self.sink_offset = 10
        self.fail_source = False

    def list_connectors(self): return sorted(self.connectors)
    def exists(self, name): return name in self.connectors
    def get_config(self, name): return self.connectors[name]
    def put_config(self, name, config): self.puts.append(name); self.connectors[name] = dict(config); return dict(config)
    def status(self, name):
        state = "FAILED" if (self.fail_source and name.endswith("source")) else "RUNNING"
        return {"connector": {"state": state}, "tasks": [{"state": state}]}
    def is_running(self, name):
        st = self.status(name)
        return st["connector"]["state"] == "RUNNING" and name in self.connectors, st
    def delete(self, name):
        self.deletes.append(name)
        return self.connectors.pop(name, None) is not None
    def offsets(self, name):
        if not self.offsets_supported:
            return None
        return {"offsets": [{"partition": {"kafka_topic": "t", "kafka_partition": 0}, "offset": {"kafka_offset": self.sink_offset}}]}


class FakeDb:
    def __init__(self) -> None:
        self.rows = {"onprem": 1010, "aws": 0}
        self.seed = {"onprem": 1000, "aws": 0}
        self.app_insert = {"onprem": True, "aws": False}
        self.terminated = 0
        self.calls: list[str] = []
        self.target_rows: set[str] = set()
        self.fence_works = True
        self.watermark: str | None = "2026-01-01T00:00:00+00:00"  # seed 행은 기준점 이전, 그 뒤 워커 행은 delta

    def precheck(self, target, *, is_source):
        return {"table_exists": True, "app_insert": self.app_insert[target], "wal_level": "logical",
                "cdc_replication": True, "row_count": self.rows[target], "identity": {"database": "migration_demo"}}
    def app_can_insert(self, target): return self.app_insert[target]
    def revoke_app_insert(self, target): self.calls.append(f"revoke:{target}"); self.app_insert[target] = False
    def grant_app_insert(self, target): self.calls.append(f"grant:{target}"); self.app_insert[target] = True
    def terminate_app_sessions(self, target): self.terminated += 1; return 1
    def test_insert_rejected(self, target, request_id): return self.fence_works and not self.app_insert[target]
    def count_rows(self, target, phase=None): return self.seed[target] if phase == "seed" else self.rows[target]
    def max_created_at(self, target): return self.watermark
    def count_rows_in_scope(self, target, *, since=None, upto=None):
        # 기준점 이전 = seed 집합, 기준점 이후 = 나머지. count_rows 는 테스트가 바꿔치기할 수 있어 self 로 부른다.
        if upto is not None:
            return self.seed[target]
        return self.count_rows(target) - self.seed[target]
    def row_exists(self, target, request_id): return request_id in self.target_rows or request_id.startswith("src-")
    def latest_ids(self, target, n, *, since=None): return [f"src-{i}" for i in range(n)]
    def prepare_source(self, target): self.calls.append("prepare"); return {"publication_created": True}
    def drop_source_slot(self, target):
        self.calls.append(f"drop_slot:{target}")
        if getattr(self, "drop_fails", False):
            raise RuntimeError("슬롯이 아직 활성 상태다")
        return {"slot": "forward_001", "slot_dropped": True, "publication": "forward_001_pub", "publication_dropped": True}


class FakeVerify:
    def __init__(self) -> None:
        self.results: list[bool] = []
        self.runs: list[tuple[str, str]] = []

    def run(self, mode, label, *, since=None, upto=None, method="rows"):
        self.runs.append((mode, label))
        self.scopes = getattr(self, "scopes", []) + [(label, since, upto, method)]
        if upto is not None:
            # 기준점 이전(스냅샷) 집합 digest 검증. 기존 테스트의 results 는 delta 검증(pre-stop, post-stop,
            # policy-pre-grant, final) 순서를 뜻하므로 여기서는 소비하지 않고 snapshot_results 를 따로 쓴다.
            ok = self.snapshot_results.pop(0) if getattr(self, "snapshot_results", None) else True
            return {"passed": ok, "counts": {}, "report": f"/r/{label}.json", "method": method,
                    "source_count": 1000, "target_count": 1000}
        ok = self.results.pop(0) if self.results else True
        return {"passed": ok, "counts": {}, "report": f"/r/{label}.json", "method": method,
                "source_count": 1000 if upto is not None else 10, "target_count": 1000 if upto is not None else 10}


@pytest.fixture
def fakes(config):
    return {"worker": FakeWorker(config.job.source), "connect": FakeConnect(), "db": FakeDb(), "verify": FakeVerify()}


def _build(config, state_dir, fakes, approve=None, clock=None):
    ports = Ports(worker=fakes["worker"], connect=fakes["connect"], db=fakes["db"], verify=fakes["verify"])
    docs = {"source": {"name": config.job.source_connector, "config": {"a": "1"}},
            "sink": {"name": config.job.sink_connector, "config": {"b": "2"}}}
    ticks = {"t": 0.0}

    def fake_clock():
        ticks["t"] += 1.0
        return ticks["t"]

    return Cutover(config, CutoverStore(state_dir), ports, connector_docs=docs, approve=approve,
                   sleep=lambda s: None, clock=clock or fake_clock,
                   policy_gate=lambda stage, checks: {"plan_digest": "test", "execution_digest": "test"})


def _grow_target_commits(fakes):
    """전환 후 워커가 대상에 커밋하는 것을 흉내: status 조회마다 1건 증가."""
    w = fakes["worker"]
    orig_status = w.status

    def status():
        if w.target == "aws" and w.state == "RUNNING":
            w.committed["aws"] += 1
        return orig_status()

    w.status = status


def _simulate_replication(fakes):
    """snapshot 과 CDC 를 흉내: 호출될 때마다 대상 행 수가 원본을 따라간다."""
    db, connect = fakes["db"], fakes["connect"]
    orig_count = db.count_rows

    def count_rows(target, phase=None):
        if connect.connectors:
            db.seed["aws"] = db.seed["onprem"]
            db.rows["aws"] = db.rows["onprem"]
            db.target_rows.add("last-1")
        if target == "onprem" and phase is None and fakes["worker"].state == "RUNNING" and fakes["worker"].target == "onprem":
            db.rows["onprem"] += 1
        return orig_count(target, phase)

    db.count_rows = count_rows


def test_full_run_stops_at_ready_then_completes_with_approval(config, state_dir, fakes):
    _simulate_replication(fakes)
    cut = _build(config, state_dir, fakes)
    with pytest.raises(ApprovalRequired):
        cut.run()
    assert cut.job.stage == READY
    assert fakes["connect"].puts == [config.job.source_connector, config.job.sink_connector]
    _grow_target_commits(fakes)

    cut2 = _build(config, state_dir, fakes, approve="meeting-42")
    assert cut2.run() == COMPLETED
    w, db, connect, verify = fakes["worker"], fakes["db"], fakes["connect"], fakes["verify"]
    assert w.calls == ["pause", "drain", "switch:aws", "resume"]
    assert db.calls == ["prepare", "revoke:onprem", "grant:aws", "drop_slot:onprem"]
    assert cp_cleanup(cut2)["slot_dropped"] is True
    assert db.terminated == 1
    assert connect.deletes == [config.job.source_connector, config.job.sink_connector]
    assert verify.runs == [("pre", "snapshot"), ("pre", "pre-stop"), ("pre", "post-stop"),
                           ("pre", "policy-pre-grant"), ("post", "final"), ("pre", "final-snapshot")]
    wm = fakes["db"].watermark
    # 스냅샷 집합은 기준점까지 digest 로, 전환 구간 비교는 모두 기준점 이후 행만 본다
    assert [(label, since, upto, method) for label, since, upto, method in verify.scopes] == [
        ("snapshot", None, wm, "digest"), ("pre-stop", wm, None, "rows"), ("post-stop", wm, None, "rows"),
        ("policy-pre-grant", wm, None, "rows"), ("final", wm, None, "rows"), ("final-snapshot", None, wm, "digest")]
    assert cut.job.checkpoint["watermark"] == wm
    assert cut.job.checkpoint["snapshot_set"]["passed"] and cut.job.checkpoint["snapshot_set"]["count"] == 1000
    # 순서: REVOKE 는 삭제 전에, GRANT 는 삭제·재검증 후에
    assert connect.connectors == {}
    cp = cut2.job.checkpoint
    assert cp["approval"]["id"] == "meeting-42" and cp["fenced"]["last_committed"]["request_id"] == "last-1"
    assert cp["verify_final"]["passed"] and "sink_offsets" in cp["drained"]
    steps = [s for s, _ in cut2.store.steps(config.job.job_id)]
    assert "grant_target_app_insert" in steps and "delete_source_connector" in steps


def cp_cleanup(cut):
    return cut.job.checkpoint["source_cleanup"]


def test_precheck_fails_when_worker_does_not_know_target(config, state_dir, fakes):
    """워커 ConfigMap 에 대상 target 이 없으면 SWITCHING 에서 터지기 전에 PRECHECK 가 막는다 (귀환 onprem_return 사례)."""
    fakes["worker"].known_targets = ["onprem"]
    cut = _build(config, state_dir, fakes)
    with pytest.raises(CutoverError, match="워커 설정에 target aws"):
        cut.run()
    assert cut.job.stage == ABORTED and cut.job.checkpoint["failed_stage"] == PRECHECK


def test_old_worker_without_known_targets_still_passes_precheck(config, state_dir, fakes):
    w = fakes["worker"]
    orig = w.status
    w.status = lambda: {k: v for k, v in orig().items() if k != "known_targets"}
    _simulate_replication(fakes)
    with pytest.raises(ApprovalRequired):
        _build(config, state_dir, fakes).run()


def test_slot_cleanup_failure_does_not_fail_cutover(config, state_dir, fakes):
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    fakes["db"].drop_fails = True
    cut = _build(config, state_dir, fakes, approve="a")
    assert cut.run() == COMPLETED
    assert "error" in cp_cleanup(cut)


def test_slot_cleanup_can_be_disabled(state_dir, fakes):
    from migration.config import parse_config
    from tests.conftest import RAW_CONFIG
    raw = {**RAW_CONFIG, "cutover": {**RAW_CONFIG.get("cutover", {}), "drop_source_slot_on_complete": False}}
    config = parse_config(raw)
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    cut = _build(config, state_dir, fakes, approve="a")
    assert cut.run() == COMPLETED
    assert cp_cleanup(cut) == {"skipped": True} and "drop_slot:onprem" not in fakes["db"].calls


def test_copying_sees_seed_inserted_after_stage_start(config, state_dir, fakes):
    """seed 를 COPYING 시작 뒤에 넣어도 스냅샷 게이트가 열린다 (리허설에서 겪은 타임아웃 재현)."""
    db = fakes["db"]
    db.seed = {"onprem": 0, "aws": 0}
    polls = {"n": 0}
    orig = db.count_rows

    def count_rows(target, phase=None):
        if phase == "seed":
            polls["n"] += 1
            if polls["n"] > 2:  # 두 번째 폴링 뒤에 seed 가 양쪽에 들어온 상황
                db.seed = {"onprem": 1000, "aws": 1000}
        return orig(target, phase)

    db.count_rows = count_rows
    _simulate_replication(fakes)
    with pytest.raises(ApprovalRequired):
        _build(config, state_dir, fakes).run()
    assert polls["n"] > 2


def test_rerun_does_not_repeat_external_steps(config, state_dir, fakes):
    _simulate_replication(fakes)
    cut = _build(config, state_dir, fakes, approve="a")
    _grow_target_commits(fakes)
    assert cut.run() == COMPLETED
    puts, deletes, calls = list(fakes["connect"].puts), list(fakes["connect"].deletes), list(fakes["worker"].calls)
    cut2 = _build(config, state_dir, fakes, approve="a")
    assert cut2.run() == COMPLETED
    assert fakes["connect"].puts == puts and fakes["connect"].deletes == deletes and fakes["worker"].calls == calls


def test_precheck_fails_when_target_app_writable(config, state_dir, fakes):
    fakes["db"].app_insert["aws"] = True
    cut = _build(config, state_dir, fakes)
    with pytest.raises(CutoverError, match="PRECHECK"):
        cut.run()
    assert cut.job.stage == ABORTED and cut.job.checkpoint["failed_stage"] == PRECHECK
    # ABORTED 는 --retry 없이는 재개하지 않는다
    with pytest.raises(CutoverError, match="--retry"):
        _build(config, state_dir, fakes).run()
    fakes["db"].app_insert["aws"] = False
    _simulate_replication(fakes)
    cut3 = _build(config, state_dir, fakes)
    with pytest.raises(ApprovalRequired):
        cut3.run(retry=True)
    assert cut3.job.stage == READY


def test_fence_failure_aborts_before_target_write(config, state_dir, fakes):
    _simulate_replication(fakes)
    fakes["db"].fence_works = False
    cut = _build(config, state_dir, fakes, approve="x")
    with pytest.raises(CutoverError, match="FENCING"):
        cut.run()
    assert cut.job.stage == ABORTED and cut.job.checkpoint["failed_stage"] == FENCING
    assert fakes["db"].app_insert["aws"] is False and "grant:aws" not in fakes["db"].calls
    # 원복: 커넥터 삭제, 원본 앱 INSERT 복구, 워커 재개 (target 은 여전히 원본)
    out = _build(config, state_dir, fakes).abort_rollback()
    assert out["source_app_insert"] is True and out["worker"] == "RUNNING"
    assert fakes["worker"].target == "onprem"
    assert _build(config, state_dir, fakes).job.stage == ROLLED_BACK


def test_failure_after_target_grant_is_needs_recovery(config, state_dir, fakes):
    _simulate_replication(fakes)
    fakes["verify"].results = [True, True, True, False]  # 마지막 post 검증 실패
    _grow_target_commits(fakes)
    cut = _build(config, state_dir, fakes, approve="x")
    with pytest.raises(CutoverError, match=NEEDS_RECOVERY):
        cut.run()
    assert cut.job.stage == NEEDS_RECOVERY and cut.job.checkpoint["failed_stage"] == VERIFYING
    with pytest.raises(CutoverError, match="force-retry"):
        _build(config, state_dir, fakes, approve="x").run(retry=True)
    with pytest.raises(CutoverError, match="ABORTED 상태에서만"):
        _build(config, state_dir, fakes).abort_rollback()


def test_connector_failed_aborts_during_copy(config, state_dir, fakes):
    fakes["connect"].fail_source = True
    cut = _build(config, state_dir, fakes)
    with pytest.raises(CutoverError, match="FAILED"):
        cut.run()
    assert cut.job.stage == ABORTED and cut.job.checkpoint["failed_stage"] == COPYING


def test_offsets_unsupported_falls_back_with_note(config, state_dir, fakes):
    _simulate_replication(fakes)
    fakes["connect"].offsets_supported = False
    _grow_target_commits(fakes)
    cut = _build(config, state_dir, fakes, approve="x")
    assert cut.run() == COMPLETED
    assert "미지원" in cut.job.checkpoint["offset_note"]


def test_direction_mismatch_rejected(config, state_dir, fakes):
    from migration.config import ConfigError
    from migration.state import CutoverJob
    store = CutoverStore(state_dir)
    store.save(CutoverJob(config.job.job_id, "aws->onprem", "aws", "onprem", SWITCHING, {}, ""))
    with pytest.raises(ConfigError):
        _build(config, state_dir, fakes)


def test_plan_lists_all_stages(config, state_dir, fakes):
    names = [n for n, _ in _build(config, state_dir, fakes).plan()]
    assert names == [PRECHECK, COPYING, CATCHING_UP, READY, FENCING, "DRAINING", "SYNC_CONFIRMED", SWITCHING,
                     VERIFYING, COMPLETED]


def test_policy_hold_before_grant_restores_source(config, state_dir, fakes):
    _simulate_replication(fakes)
    cut = _build(config, state_dir, fakes, approve="x")
    original = cut.policy_gate
    def gate(stage, checks=None):
        if stage == "final_verify":
            raise RuntimeError("policy hold")
        return original(stage, checks)
    cut.policy_gate = gate
    with pytest.raises(CutoverError, match=ROLLED_BACK):
        cut.run()
    assert cut.job.stage == ROLLED_BACK
    assert fakes["db"].app_insert == {"onprem": True, "aws": False}
    assert fakes["worker"].state == "RUNNING"
    assert "grant:aws" not in fakes["db"].calls


def test_grant_lost_response_never_reopens_source(config, state_dir, fakes):
    _simulate_replication(fakes)
    original = fakes["db"].grant_app_insert
    def lost_response(target):
        original(target)
        raise ConnectionError("grant response lost")
    fakes["db"].grant_app_insert = lost_response
    cut = _build(config, state_dir, fakes, approve="x")
    with pytest.raises(CutoverError, match=NEEDS_RECOVERY):
        cut.run()
    assert cut.job.checkpoint["target_grant_attempted"]
    assert not fakes["db"].app_insert["onprem"]
    assert fakes["db"].app_insert["aws"]
    # Re-closing target later cannot erase the possibility of target writes.
    fakes["db"].app_insert["aws"] = False
    with pytest.raises(CutoverError):
        _build(config, state_dir, fakes).abort_rollback()


def test_legacy_switching_failure_requires_manual_recovery(config, state_dir, fakes):
    cut = _build(config, state_dir, fakes, approve="x")
    cut.job.checkpoint.clear()
    cut._save(SWITCHING)
    cut.policy_gate = lambda *args: (_ for _ in ()).throw(RuntimeError("hold"))
    with pytest.raises(CutoverError, match=NEEDS_RECOVERY):
        cut.run()
    assert "grant:onprem" not in fakes["db"].calls


# ---- 기준점(watermark) 기반 판정 ------------------------------------------------


def test_snapshot_set_is_verified_once_and_rechecked_only_when_its_count_changes(config, state_dir, fakes):
    """CATCHING_UP 에서 스냅샷 집합을 digest 로 한 번 확정하고, DRAINING 에서 기준점 이전 건수가 변했을 때만 다시 본다."""
    _simulate_replication(fakes)
    db, verify = fakes["db"], fakes["verify"]
    cut = _build(config, state_dir, fakes, approve="a")
    _grow_target_commits(fakes)
    # 스냅샷 집합 확정 뒤 늦게 커밋된 행이 기준점 이전 집합에 들어온 상황: DRAINING 의 건수 비교가 바뀐다
    orig = db.count_rows_in_scope
    def count_rows_in_scope(target, *, since=None, upto=None):
        if upto is not None and cut.job.stage == DRAINING:
            return db.seed[target] + 1
        return orig(target, since=since, upto=upto)
    db.count_rows_in_scope = count_rows_in_scope
    assert cut.run() == COMPLETED
    labels = [label for _, label in verify.runs]
    assert labels == ["snapshot", "snapshot-recheck", "pre-stop", "post-stop", "policy-pre-grant", "final",
                      "final-snapshot"]
    assert cut.job.checkpoint["snapshot_recheck"]["needed"] and cut.job.checkpoint["snapshot_set"]["count"] == 1001
    assert cut.job.checkpoint["drained"]["delta_source"] == cut.job.checkpoint["drained"]["delta_target"]


def test_snapshot_verify_waits_until_sink_applies_everything(config, state_dir, fakes):
    """스냅샷 집합 digest 가 아직 안 맞으면(싱크 적용 중) 실패가 아니라 대기다."""
    _simulate_replication(fakes)
    verify = fakes["verify"]
    verify.snapshot_results = [False, False, True]
    with pytest.raises(ApprovalRequired):
        _build(config, state_dir, fakes).run()
    assert [label for _, label in verify.runs] == ["snapshot", "snapshot", "snapshot"]


def test_late_row_in_snapshot_set_that_never_reaches_target_aborts(config, state_dir, fakes):
    _simulate_replication(fakes)
    db, verify = fakes["db"], fakes["verify"]
    cut = _build(config, state_dir, fakes, approve="a")
    orig = db.count_rows_in_scope
    def count_rows_in_scope(target, *, since=None, upto=None):
        if upto is not None and cut.job.stage == DRAINING:
            return db.seed[target] + 1
        return orig(target, since=since, upto=upto)
    db.count_rows_in_scope = count_rows_in_scope
    verify.snapshot_results = [True, False]  # 최초 확정은 통과, 재확정 실패
    with pytest.raises(CutoverError, match="늦게 들어온 행"):
        cut.run()
    assert cut.job.stage == ABORTED


def test_empty_source_at_start_has_no_snapshot_set(config, state_dir, fakes):
    """빈 테이블에서 시작하면 기준점이 None 이고 모든 행이 delta 라 스냅샷 digest 는 건너뛴다."""
    _simulate_replication(fakes)
    db, verify = fakes["db"], fakes["verify"]
    db.watermark = None
    db.seed = {"onprem": 1, "aws": 0}  # COPYING 게이트는 seed>0 을 요구한다
    cut = _build(config, state_dir, fakes, approve="a")
    _grow_target_commits(fakes)
    assert cut.run() == COMPLETED
    assert cut.job.checkpoint["watermark"] is None
    assert all(upto is None for _, _, upto, _ in verify.scopes)
    assert cut.job.checkpoint["snapshot_set"]["passed"]


def test_job_without_watermark_compares_everything_as_delta(config, state_dir, fakes):
    """기준점 도입 전 체크포인트를 이어서 실행하면 since=None 으로 전체를 비교한다 (느리지만 안전)."""
    cut = _build(config, state_dir, fakes)
    cut._save(FENCING, fence_started_at=1)
    assert cut.watermark is None
    fakes["db"].app_insert["onprem"] = False
    cut.stage_switching = lambda: None
    cut.stage_sync_confirmed()
    assert [s for _, s, _, _ in fakes["verify"].scopes] == [None, None]
