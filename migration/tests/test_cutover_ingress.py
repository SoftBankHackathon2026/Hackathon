"""cutover.py 가 접수 서비스(durable-ingress)를 워커와 같은 순서로 제어하는지 가짜 포트로 검증한다."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from migration.config import ConfigError, parse_config
from migration.cutover import ABORTED, COMPLETED, FENCING, ROLLED_BACK, SWITCHING, Cutover, CutoverError, Ports
from migration.state import CutoverStore

from tests.conftest import RAW_CONFIG
from tests.test_cutover import FakeConnect, FakeDb, FakeVerify, FakeWorker, _grow_target_commits, _simulate_replication

INGRESS_RAW = {
    "control_url": "http://durable-ingress-control:9092",
    "targets": {"onprem": "onprem", "aws": "aws_db"},
    "app_checks": {"aws": {"url": "http://sample-app-rds", "environment": "aws", "database": "migration_demo"}},
}


class FakeIngress:
    """durable-ingress 제어 API 의 규칙(paused 일 때만 target, epoch 일치, 재시작은 paused)을 흉내 낸다."""

    def __init__(self, target: str = "onprem") -> None:
        self.mode = "running"
        self.target = target
        self.epoch = 0
        self.in_flight = 0
        self.calls: list[str] = []
        self.settled = True
        self.app_ready_after = 0   # check_app 이 ok 를 돌려주기까지 필요한 호출 수
        self.app_checks = 0

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "target": self.target, "epoch": str(self.epoch), "in_flight": self.in_flight,
                "last_error": None}

    def pause(self) -> dict[str, Any]:
        self.calls.append("pause")
        self.mode = "paused"
        return {**self.status(), "settled": self.settled}

    def set_target(self, target: str, expected_epoch: str) -> dict[str, Any]:
        self.calls.append(f"target:{target}@{expected_epoch}")
        assert self.mode == "paused", "PAUSE_REQUIRED"
        assert expected_epoch == str(self.epoch), "EPOCH_CONFLICT"
        self.target = target
        self.epoch += 1
        return self.status()

    def resume(self, expected_epoch: str) -> dict[str, Any]:
        self.calls.append(f"resume@{expected_epoch}")
        assert expected_epoch == str(self.epoch), "EPOCH_CONFLICT"
        self.mode = "running"
        return self.status()

    def check_app(self, job_target: str) -> dict[str, Any]:
        self.app_checks += 1
        self.calls.append(f"check_app:{job_target}")
        ok = self.app_checks > self.app_ready_after
        return {"ok": ok, "problems": [] if ok else ["writes_paused=true"], "url": "http://sample-app-rds"}


@pytest.fixture
def iconfig():
    raw = copy.deepcopy(RAW_CONFIG)
    raw["ingress"] = copy.deepcopy(INGRESS_RAW)
    return parse_config(raw)


@pytest.fixture
def ifakes(iconfig):
    return {"worker": FakeWorker(iconfig.job.source), "connect": FakeConnect(), "db": FakeDb(), "verify": FakeVerify(),
            "ingress": FakeIngress()}


def _build(config, state_dir, fakes, approve=None):
    ports = Ports(worker=fakes["worker"], connect=fakes["connect"], db=fakes["db"], verify=fakes["verify"],
                  ingress=fakes["ingress"])
    docs = {"source": {"name": config.job.source_connector, "config": {"a": "1"}},
            "sink": {"name": config.job.sink_connector, "config": {"b": "2"}}}
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 1.0
        return ticks["t"]

    return Cutover(config, CutoverStore(state_dir), ports, connector_docs=docs, approve=approve,
                   sleep=lambda s: None, clock=clock,
                   policy_gate=lambda stage, checks: {"plan_digest": "test", "execution_digest": "test"})


def _order(calls: list[str], first: str, second: str) -> None:
    assert first in calls and second in calls, calls
    assert calls.index(first) < calls.index(second), calls


def test_full_run_pauses_queue_before_fence_and_switches_after_grant(iconfig, state_dir, ifakes):
    _simulate_replication(ifakes)
    _grow_target_commits(ifakes)
    ing, db = ifakes["ingress"], ifakes["db"]
    # 접수 서비스 pause 와 DB REVOKE 의 순서를 한 목록에서 비교하기 위해 db 호출을 ingress.calls 에도 남긴다
    orig_revoke, orig_grant = db.revoke_app_insert, db.grant_app_insert
    db.revoke_app_insert = lambda t: (ing.calls.append(f"db:revoke:{t}"), orig_revoke(t))[1]
    db.grant_app_insert = lambda t: (ing.calls.append(f"db:grant:{t}"), orig_grant(t))[1]

    cut = _build(iconfig, state_dir, ifakes)
    with pytest.raises(Exception):  # READY: 승인 필요
        cut.run()
    assert ing.mode == "running" and ing.calls == []   # 승인 전에는 큐를 건드리지 않는다

    cut = _build(iconfig, state_dir, ifakes, approve="ops-1")
    assert cut.run() == COMPLETED
    _order(ing.calls, "pause", "db:revoke:onprem")                     # 큐 정지 → 원본 차단
    _order(ing.calls, "db:grant:aws", "check_app:aws")                 # 대상 열림 → 앱 확인
    _order(ing.calls, "check_app:aws", "target:aws_db@0")              # 앱 확인 → 큐 대상 변경
    _order(ing.calls, "target:aws_db@0", "resume@1")                   # 새 epoch 로 재개
    assert ing.mode == "running" and ing.target == "aws_db" and ing.epoch == 1
    job = cut.store.load(iconfig.job.job_id)
    assert job.checkpoint["precheck"]["ingress"]["target"] == "onprem"
    assert job.checkpoint["fenced"]["ingress"]["settled"] is True
    assert job.checkpoint["ingress"]["target"]["target"] == "aws_db"
    assert job.checkpoint["post_switch"]["ingress"]["mode"] == "running"


def test_precheck_fails_when_queue_points_elsewhere(iconfig, state_dir, ifakes):
    ifakes["ingress"].target = "aws_db"
    cut = _build(iconfig, state_dir, ifakes)
    with pytest.raises(CutoverError, match="접수 서비스 target"):
        cut.run()
    assert cut.job.stage == ABORTED


def test_unsettled_pause_aborts_before_source_revoke(iconfig, state_dir, ifakes):
    _simulate_replication(ifakes)
    ifakes["ingress"].settled = False
    cut = _build(iconfig, state_dir, ifakes, approve="ops-1")
    with pytest.raises(CutoverError, match="settled"):
        cut.run()
    assert cut.job.stage == ABORTED and cut.job.checkpoint["failed_stage"] == FENCING
    assert "revoke:onprem" not in ifakes["db"].calls          # 원본은 아직 열려 있다
    assert ifakes["db"].app_insert["onprem"] is True


def test_switch_waits_until_target_app_ready(iconfig, state_dir, ifakes):
    _simulate_replication(ifakes)
    _grow_target_commits(ifakes)
    ifakes["ingress"].app_ready_after = 2       # 두 번은 writes_paused, 세 번째에 ok
    cut = _build(iconfig, state_dir, ifakes, approve="ops-1")
    assert cut.run() == COMPLETED
    assert ifakes["ingress"].app_checks == 3
    assert ifakes["ingress"].target == "aws_db"


def test_target_app_never_ready_is_needs_recovery_and_queue_stays_paused(iconfig, state_dir, ifakes):
    _simulate_replication(ifakes)
    ifakes["ingress"].app_ready_after = 10_000
    cut = _build(iconfig, state_dir, ifakes, approve="ops-1")
    with pytest.raises(CutoverError, match="target_app_ready"):
        cut.run()
    assert cut.job.stage == "NEEDS_RECOVERY" and cut.job.checkpoint["failed_stage"] == SWITCHING
    assert ifakes["ingress"].mode == "paused" and ifakes["ingress"].target == "onprem"


def test_rerun_does_not_repeat_queue_calls(iconfig, state_dir, ifakes):
    _simulate_replication(ifakes)
    _grow_target_commits(ifakes)
    cut = _build(iconfig, state_dir, ifakes, approve="ops-1")
    assert cut.run() == COMPLETED
    n = len(ifakes["ingress"].calls)
    assert _build(iconfig, state_dir, ifakes, approve="ops-1").run() == COMPLETED
    assert len(ifakes["ingress"].calls) == n


def test_abort_rollback_resumes_queue_on_source(iconfig, state_dir, ifakes):
    _simulate_replication(ifakes)
    ifakes["db"].fence_works = False            # FENCING 에서 실패 → ABORTED (큐는 paused 상태)
    cut = _build(iconfig, state_dir, ifakes, approve="ops-1")
    with pytest.raises(CutoverError):
        cut.run()
    assert cut.job.stage == ABORTED and ifakes["ingress"].mode == "paused"
    out = _build(iconfig, state_dir, ifakes).abort_rollback()
    assert out["ingress"]["mode"] == "running" and ifakes["ingress"].target == "onprem"
    assert cut.store.load(iconfig.job.job_id).stage == ROLLED_BACK


def test_abort_rollback_refuses_when_queue_already_moved(iconfig, state_dir, ifakes):
    _simulate_replication(ifakes)
    ifakes["db"].fence_works = False
    cut = _build(iconfig, state_dir, ifakes, approve="ops-1")
    with pytest.raises(CutoverError):
        cut.run()
    ifakes["ingress"].target = "aws_db"         # 누가 손으로 옮겨 둔 상황
    with pytest.raises(CutoverError, match="수동 확인"):
        _build(iconfig, state_dir, ifakes).abort_rollback()


def test_without_ingress_section_queue_is_untouched(config, state_dir, ifakes):
    """ingress 절이 없으면 예전처럼 워커·DB 만 제어한다 (수동 운영)."""
    _simulate_replication(ifakes)
    _grow_target_commits(ifakes)
    ports = Ports(worker=ifakes["worker"], connect=ifakes["connect"], db=ifakes["db"], verify=ifakes["verify"])
    docs = {"source": {"name": config.job.source_connector, "config": {}}, "sink": {"name": config.job.sink_connector, "config": {}}}
    cut = Cutover(config, CutoverStore(state_dir), ports, connector_docs=docs, approve="ops-1", sleep=lambda s: None,
                  policy_gate=lambda stage, checks: {"plan_digest": "t", "execution_digest": "t"})
    assert config.ingress.enabled is False
    assert cut.run() == COMPLETED
    assert ifakes["ingress"].calls == []


def test_plan_mentions_queue_only_when_configured(iconfig, config, state_dir, ifakes):
    with_q = dict(_build(iconfig, state_dir, ifakes).plan())
    assert "접수 큐" in with_q[FENCING] and "접수 큐" in with_q[SWITCHING]
    ports = Ports(worker=ifakes["worker"], connect=ifakes["connect"], db=ifakes["db"], verify=ifakes["verify"])
    without = dict(Cutover(config, CutoverStore(state_dir), ports, connector_docs={},
                           policy_gate=lambda s, c: {}).plan())
    assert "접수 큐" not in without[FENCING]


@pytest.mark.parametrize("ingress_raw, msg", [
    ({"control_url": "http://x:9092"}, "ingress.targets"),
    ({"control_url": "http://x:9092", "targets": {"onprem": "onprem", "gcp": "x"}}, "targets 에 없다"),
    ({"targets": {"onprem": "onprem"}, "app_checks": {"aws": {"environment": "aws"}}}, "url"),
    ({"bogus": 1}, "알 수 없는 키"),
])
def test_ingress_config_validation(ingress_raw, msg):
    raw = copy.deepcopy(RAW_CONFIG)
    raw["ingress"] = ingress_raw
    with pytest.raises(ConfigError, match=msg):
        parse_config(raw)


def test_ingress_target_mapping_missing_fails_precheck(state_dir, ifakes):
    raw = copy.deepcopy(RAW_CONFIG)
    raw["ingress"] = {"control_url": "http://x:9092", "targets": {"onprem": "onprem"}}   # aws 매핑 없음
    cfg = parse_config(raw)
    cut = _build(cfg, state_dir, ifakes)
    with pytest.raises(CutoverError, match="ingress.targets 에 aws"):
        cut.run()
