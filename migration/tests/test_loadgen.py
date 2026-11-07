"""loadgen.py: 워커 상태 머신·재시도·충돌 비교·HTTP 제어 API (메모리 백엔드)."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from migration.loadgen import InsertResult, Outcome, Worker, WorkerError, WorkerSettings, WorkerState, serve
from migration.state import COMMITTED, MISMATCH, RequestRecord, RequestStore


class MemoryBackend:
    """DB 대신 dict 에 쓴다. fail_times 만큼 연결 오류를 흉내낸다."""

    def __init__(self, name: str, rows: dict[str, tuple] | None = None) -> None:
        self.name = name
        self.rows: dict[str, tuple] = rows if rows is not None else {}
        self.fail_times = 0
        self.lose_response_once = False
        self.inserts = 0
        self.lookups = 0
        self.closed = False
        self.lock = threading.Lock()

    def insert(self, rec: RequestRecord) -> InsertResult:
        with self.lock:
            if self.fail_times > 0:
                self.fail_times -= 1
                raise psycopg.OperationalError("connection lost")
            self.inserts += 1
            row = (rec.run_id, rec.phase, rec.request_no, rec.payload, datetime.fromisoformat(rec.created_at))
            if rec.request_id in self.rows:
                existing = self.rows[rec.request_id]
                if existing[:4] == row[:4] and existing[4] == row[4]:
                    return InsertResult(Outcome.EXISTS_EQUAL)
                return InsertResult(Outcome.EXISTS_DIFFERENT, "differs")
            self.rows[rec.request_id] = row
            if self.lose_response_once:
                self.lose_response_once = False
                raise psycopg.OperationalError("response lost after commit")
            return InsertResult(Outcome.INSERTED)

    def lookup(self, request_id: str):
        with self.lock:
            self.lookups += 1
            return self.rows.get(request_id)

    def identity(self) -> dict[str, Any]:
        return {"database": "migration_demo", "server_addr": f"10.0.0.{len(self.name)}"}

    def close(self) -> None:
        self.closed = True


def _settings(state_dir, **kw) -> WorkerSettings:
    base = dict(run_id="run-1", phase="onprem_live", target="onprem", rate_per_second=0,
                state_dir=state_dir, max_queue=5, max_queue_age_seconds=3600, max_consecutive_failures=3,
                max_retry_backoff_seconds=0.01)
    base.update(kw)
    return WorkerSettings(**base)


def test_demo_budget_survives_restart_and_replayed_release(state_dir):
    settings = _settings(state_dir, max_generated=200, max_queue=200)
    store = RequestStore(state_dir)
    backend = MemoryBackend('onprem')
    worker = Worker(settings, store, lambda _: backend)
    assert worker.generate_one() is None
    worker.set_generation_limit(100)
    first = [worker.generate_one() for _ in range(100)]
    assert all(first)
    assert worker.generate_one() is None
    worker.pause()
    worker.set_generation_limit(200)
    assert worker.generate_one() is None, 'paused worker cannot spend target budget'
    # Restart from SQLite and persisted metadata, without relying on runtime counters.
    restarted = Worker(settings, store, lambda _: backend)
    assert restarted.status()['generated_total'] == 100
    restarted.set_generation_limit(200)
    restarted.resume()
    assert all(restarted.generate_one() for _ in range(100))
    for _ in range(10):
        restarted.set_generation_limit(200)
        assert restarted.generate_one() is None
    assert restarted.status()['generated_total'] == 200
    with pytest.raises(WorkerError, match='persisted demo budget'):
        Worker(_settings(state_dir), store, lambda _: backend)
    for invalid in [201, -1, 100, None, True]:
        with pytest.raises(WorkerError):
            restarted.set_generation_limit(invalid)
    store.close()


def test_demo_budget_http_control(state_dir):
    store = RequestStore(state_dir)
    worker = Worker(_settings(state_dir, max_generated=200), store, lambda _: MemoryBackend('onprem'))
    server = serve(worker, '127.0.0.1', 0)
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        code, body = _http(base, 'POST', '/generation-limit', {'limit': 100})
        assert code == 200 and json.loads(body)['generation_limit'] == 100
        assert _http(base, 'POST', '/generation-limit', {'limit': 201})[0] == 409
        assert _http(base, 'POST', '/generation-limit', {})[0] == 409
    finally:
        server.shutdown(); server.server_close(); store.close()


def test_demo_full_queue_waits_without_consuming_budget(state_dir):
    store = RequestStore(state_dir)
    worker = Worker(_settings(state_dir, max_generated=200, max_queue=1), store, lambda _: MemoryBackend('onprem'))
    worker.set_generation_limit(100)
    assert worker.generate_one() is not None
    assert worker.generate_one() is None
    assert worker.status()['generated_total'] == 1
    assert worker.counters['queue_rejected'] == 0
    store.close()


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def backends():
    return {"onprem": MemoryBackend("onprem"), "aws": MemoryBackend("aws")}


@pytest.fixture
def worker(state_dir, backends):
    store = RequestStore(state_dir)
    w = Worker(_settings(state_dir), store, lambda name: backends[name])
    w.start()
    yield w
    w.stop()
    store.close()


def test_generate_persists_before_send_and_commits(worker, backends):
    rec = worker.generate_one()
    assert rec is not None
    assert _wait(lambda: worker.store.get(rec.request_id).status == COMMITTED)
    assert rec.request_id in backends["onprem"].rows
    st = worker.status()
    assert st["counters"]["committed_new"] == 1 and st["committed_by_target"] == {"onprem": 1}
    assert st["latency_seconds"]["p50"] is not None and st["latency_seconds"]["samples"] == 1


def test_conflict_equal_is_committed_and_different_is_mismatch(state_dir, backends):
    store = RequestStore(state_dir)
    w = Worker(_settings(state_dir), store, lambda n: backends[n])
    # 같은 ID 의 동일 행이 이미 DB 에 있는 경우
    rec = w._make_request()
    backends["onprem"].rows[rec.request_id] = (rec.run_id, rec.phase, rec.request_no, rec.payload,
                                               datetime.fromisoformat(rec.created_at))
    store.record_pending(rec)
    w._send_until_settled(rec)
    assert store.get(rec.request_id).status == COMMITTED
    assert w.counters["committed_existing"] == 1
    # 같은 ID 인데 payload 가 다른 경우
    rec2 = w._make_request()
    backends["onprem"].rows[rec2.request_id] = (rec2.run_id, rec2.phase, rec2.request_no, "other",
                                                datetime.fromisoformat(rec2.created_at))
    store.record_pending(rec2)
    w._send_until_settled(rec2)
    assert store.get(rec2.request_id).status == MISMATCH
    assert w.counters["mismatch"] == 1
    store.close()


def test_retry_after_connection_error_keeps_same_id(worker, backends):
    backends["onprem"].fail_times = 2
    rec = worker.generate_one()
    assert _wait(lambda: worker.store.get(rec.request_id).status == COMMITTED)
    got = worker.store.get(rec.request_id)
    assert got.attempts == 3
    assert worker.counters["retries"] == 2
    assert len(backends["onprem"].rows) == 1


def test_lost_response_is_settled_by_lookup_not_duplicate(worker, backends):
    backends["onprem"].lose_response_once = True
    rec = worker.generate_one()
    assert _wait(lambda: worker.store.get(rec.request_id).status == COMMITTED)
    assert worker.drain(1)["settled"]
    assert backends["onprem"].lookups >= 1
    assert worker.counters["committed_existing"] == 1 and worker.counters["committed_new"] == 0
    assert len(backends["onprem"].rows) == 1


def test_error_state_after_consecutive_failures_then_resume(worker, backends):
    backends["onprem"].fail_times = 100
    rec = worker.generate_one()
    assert _wait(lambda: worker.state == WorkerState.ERROR)
    assert worker.store.get(rec.request_id).status == "pending"
    assert worker.queue_length() == 1  # 요청은 대기열에 남는다
    backends["onprem"].fail_times = 0
    worker.resume()
    assert _wait(lambda: worker.store.get(rec.request_id).status == COMMITTED)
    assert "aws" not in worker.store.counts_by_target()  # 반대 DB 에 쓰지 않았다
    assert backends["aws"].rows == {}


def test_pause_queues_only_and_switch_requires_paused(worker, backends):
    with pytest.raises(WorkerError):
        worker.switch_target("aws")
    first = worker.generate_one()
    assert _wait(lambda: worker.store.get(first.request_id).status == COMMITTED)
    worker.pause()
    rec = worker.generate_one()
    time.sleep(0.1)
    assert worker.store.get(rec.request_id).status == "pending"
    assert worker.queue_length() == 1
    drained = worker.drain(timeout=1)
    assert drained["settled"] and drained["queue_length"] == 1
    worker.switch_target("aws", "aws_live")
    assert backends["onprem"].closed
    assert worker.target == "aws" and worker.phase == "aws_live" and worker.state == WorkerState.PAUSED
    meta = json.loads((worker.settings.state_dir / "worker.json").read_text())
    assert meta["target"] == "aws"
    worker.resume()
    assert _wait(lambda: worker.store.get(rec.request_id).status == COMMITTED)
    assert worker.store.get(rec.request_id).target == "aws"
    assert rec.request_id in backends["aws"].rows and rec.request_id not in backends["onprem"].rows
    # 전환 후 생성된 요청은 새 phase
    rec2 = worker.generate_one()
    assert rec2.phase == "aws_live" and rec.phase == "onprem_live"


def test_switch_failure_keeps_old_target_and_paused(worker, backends):
    worker.pause()

    def bad_factory(name):
        raise psycopg.OperationalError("password=app-secret unreachable")

    worker._backend_factory = bad_factory
    with pytest.raises(WorkerError) as exc:
        worker.switch_target("aws")
    assert worker.state == WorkerState.PAUSED and worker.target == "onprem"


def test_queue_limit_records_rejection(worker, backends):
    worker.pause()
    made = [worker.generate_one() for _ in range(7)]
    assert sum(r is not None for r in made) == 5
    assert worker.counters["queue_rejected"] == 2
    assert worker.store.counts()["queue_rejected"] == 2


def test_restart_resumes_pending_with_same_ids(state_dir, backends):
    store = RequestStore(state_dir)
    w = Worker(_settings(state_dir), store, lambda n: backends[n])
    w.pause()
    ids = [w.generate_one().request_id for _ in range(3)]
    w.stop()
    store.close()
    store2 = RequestStore(state_dir)
    w2 = Worker(_settings(state_dir), store2, lambda n: backends[n])
    assert w2.queue_length() == 3
    assert w2.state == WorkerState.PAUSED
    w2.start()
    assert not backends["onprem"].rows
    w2.resume()
    assert _wait(lambda: all(store2.get(i).status == COMMITTED for i in ids))
    assert set(backends["onprem"].rows) == set(ids)
    assert w2._make_request().request_no == 4  # request_no 이어짐
    w2.stop()
    store2.close()


def _http(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_http_control_api(worker, backends):
    server = serve(worker, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        code, body = _http(base, "GET", "/healthz")
        assert code == 200 and json.loads(body)["ok"]
        code, body = _http(base, "POST", "/target", {"target": "aws"})
        assert code == 409  # RUNNING 에서는 거절
        code, body = _http(base, "POST", "/pause")
        assert json.loads(body)["state"] == "PAUSED"
        code, body = _http(base, "POST", "/drain", {"timeout": 1})
        assert code == 200 and json.loads(body)["settled"]
        code, body = _http(base, "POST", "/target", {"target": "aws", "phase": "aws_live"})
        assert code == 200 and json.loads(body)["target"] == "aws"
        code, body = _http(base, "POST", "/target", {"target": 5})
        assert code == 400 or code == 409
        code, body = _http(base, "POST", "/resume")
        assert json.loads(body)["state"] == "RUNNING"
        code, body = _http(base, "GET", "/status")
        st = json.loads(body)
        assert st["target"] == "aws" and st["phase"] == "aws_live"
        code, body = _http(base, "GET", "/metrics")
        assert code == 200 and 'migration_worker_state{state="RUNNING"} 1' in body
        assert 'migration_worker_target_info{target="aws",phase="aws_live"} 1' in body
        code, _ = _http(base, "GET", "/nope")
        assert code == 404
    finally:
        server.shutdown()


def test_permission_rejection_pauses_and_allows_drain(worker, backends, monkeypatch):
    def reject(rec):
        raise psycopg.errors.InsufficientPrivilege("permission denied")
    monkeypatch.setattr(backends["onprem"], "insert", reject)
    rec = worker.generate_one()
    assert _wait(lambda: worker.state == WorkerState.PAUSED)
    assert worker.drain(1)["settled"]
    assert worker.store.get(rec.request_id).status == "pending"
    worker.switch_target("aws")
    worker.resume()
    assert _wait(lambda: worker.store.get(rec.request_id).status == COMMITTED)


def test_unknown_commit_blocks_target_switch(worker, backends):
    backends["onprem"].fail_times = 100
    worker.generate_one()
    assert _wait(lambda: worker.state == WorkerState.ERROR)
    worker.pause()
    assert not worker.drain(0)["settled"]
    with pytest.raises(WorkerError):
        worker.switch_target("aws")


def test_corrupt_metadata_refuses_start(state_dir, backends):
    (state_dir / "worker.json").write_text("{broken")
    store = RequestStore(state_dir)
    with pytest.raises(WorkerError):
        Worker(_settings(state_dir), store, lambda n: backends[n])
    store.close()


def test_switch_target_and_pause_survive_restart(state_dir, backends):
    store = RequestStore(state_dir)
    w = Worker(_settings(state_dir), store, lambda n: backends[n])
    w.pause()
    w.switch_target("aws", "aws_live")
    restored = Worker(_settings(state_dir), store, lambda n: backends[n])
    assert (restored.state, restored.target, restored.phase) == (WorkerState.PAUSED, "aws", "aws_live")
    store.close()


def test_persistence_failure_prevents_resume(worker, monkeypatch):
    worker.pause()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("migration.loadgen.os.replace", fail)
    with pytest.raises(WorkerError):
        worker.resume()
    assert worker.state == WorkerState.ERROR


@pytest.mark.parametrize("state", ["ERROR", "SWITCHING", "PAUSED"])
def test_restart_never_resumes_stopped_state(state_dir, backends, state):
    (state_dir / "worker.json").write_text(json.dumps({
        "target": "aws", "phase": "aws_live", "run_id": "run-1", "state": state,
        "unresolved": None,
    }))
    store = RequestStore(state_dir)
    w = Worker(_settings(state_dir), store, lambda n: backends[n])
    assert w.state != WorkerState.RUNNING
    assert w.target == "aws"
    store.close()


def test_permission_error_after_lost_response_reconciles_commit(worker, backends, monkeypatch):
    backend = backends["onprem"]
    original = backend.insert
    def commit_then_reject(rec):
        original(rec)
        raise psycopg.errors.InsufficientPrivilege("permission denied on retry")
    monkeypatch.setattr(backend, "insert", commit_then_reject)
    rec = worker.generate_one()
    assert _wait(lambda: worker.store.get(rec.request_id).status == COMMITTED)
    assert worker.counters["committed_existing"] == 1
    assert worker.drain(0)["settled"]


def test_crash_after_commit_record_before_meta_clear(state_dir, backends):
    store = RequestStore(state_dir)
    w = Worker(_settings(state_dir), store, lambda n: backends[n])
    w.pause()
    rec = w.generate_one()
    w._unresolved = rec.request_id
    w._persist_meta()
    store.mark_committed(rec.request_id, "onprem", 1, datetime.now(UTC).isoformat())
    restored = Worker(_settings(state_dir), store, lambda n: backends[n])
    assert restored.state == WorkerState.PAUSED
    assert restored.drain(0)["settled"]
    restored.switch_target("aws")
    store.close()
