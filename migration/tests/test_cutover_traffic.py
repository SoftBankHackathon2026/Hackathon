from __future__ import annotations

import copy
from typing import Any

import pytest

from migration.config import ConfigError, parse_config
from migration.cutover import ABORTED, COMPLETED, SWITCHING, VERIFYING, Cutover, CutoverError, Ports
from migration.state import CutoverStore
from migration.traffic_client import HttpTrafficClient, TrafficClientError

from tests.conftest import RAW_CONFIG
from tests.test_cutover import FakeConnect, FakeDb, FakeVerify, FakeBot, _grow_target_commits, _simulate_replication
from tests.test_cutover_ingress import INGRESS_RAW, FakeIngress

TRAFFIC_RAW = {"app": "sample-app", "targets": {"onprem": "onprem", "aws": "aws"}}


class FakeTraffic:
    def __init__(self, target: str | None = None, calls: list[str] | None = None) -> None:
        self.target = target
        self.calls = calls if calls is not None else []
        self.fail_switch = False

    def status(self) -> dict[str, Any]:
        return {"app": "sample-app", "target": self.target}

    def switch(self, target: str) -> dict[str, Any]:
        self.calls.append(f"traffic:{target}")
        if self.fail_switch:
            raise RuntimeError("platform down")
        self.target = target
        return {"app": "sample-app", "target": target, "changed_at": "2026-11-07T00:00:00Z"}


def _config(with_ingress: bool = True):
    raw = copy.deepcopy(RAW_CONFIG)
    raw["traffic"] = copy.deepcopy(TRAFFIC_RAW)
    if with_ingress:
        raw["ingress"] = copy.deepcopy(INGRESS_RAW)
    return parse_config(raw)


def _build(config, state_dir, fakes, approve=None):
    ports = Ports(bot=fakes["bot"], connect=fakes["connect"], db=fakes["db"], verify=fakes["verify"],
                  ingress=fakes.get("ingress"), traffic=fakes["traffic"])
    docs = {"source": {"name": config.job.source_connector, "config": {"a": "1"}},
            "sink": {"name": config.job.sink_connector, "config": {"b": "2"}}}
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 1.0
        return ticks["t"]

    return Cutover(config, CutoverStore(state_dir), ports, connector_docs=docs, approve=approve,
                   sleep=lambda s: None, clock=clock,
                   policy_gate=lambda stage, checks: {"plan_digest": "test", "execution_digest": "test"})


@pytest.fixture
def fakes():
    config = _config()
    ingress = FakeIngress()
    return {"config": config, "bot": FakeBot(config.job.source), "connect": FakeConnect(), "db": FakeDb(),
            "verify": FakeVerify(), "ingress": ingress, "traffic": FakeTraffic(calls=ingress.calls)}


def test_public_domain_moves_after_target_grant_and_queue(state_dir, fakes):
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    db, calls = fakes["db"], fakes["ingress"].calls
    orig_grant = db.grant_app_insert
    db.grant_app_insert = lambda t: (calls.append(f"db:grant:{t}"), orig_grant(t))[1]

    cut = _build(fakes["config"], state_dir, fakes)
    with pytest.raises(Exception):
        cut.run()
    assert fakes["traffic"].target is None and "traffic:aws" not in calls

    cut = _build(fakes["config"], state_dir, fakes, approve="ops-1")
    assert cut.run() == COMPLETED
    assert fakes["traffic"].target == "aws"
    assert calls.index("db:grant:aws") < calls.index("traffic:aws")
    assert calls.index("resume@1") < calls.index("traffic:aws")
    job = cut.store.load(fakes["config"].job.job_id)
    assert job.checkpoint["precheck"]["traffic"] == {"target": None, "expected": {"source": "onprem", "target": "aws"}}
    assert job.checkpoint["traffic"]["target"] == "aws"
    assert job.checkpoint["post_switch"]["traffic"] == {"target": "aws"}


def test_rerun_does_not_switch_again(state_dir, fakes):
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    assert _build(fakes["config"], state_dir, fakes, approve="ops-1").run() == COMPLETED
    n = fakes["ingress"].calls.count("traffic:aws")
    assert _build(fakes["config"], state_dir, fakes, approve="ops-1").run() == COMPLETED
    assert fakes["ingress"].calls.count("traffic:aws") == n == 1


def test_precheck_refuses_when_domain_already_points_elsewhere(state_dir, fakes):
    fakes["traffic"].target = "aws"
    cut = _build(fakes["config"], state_dir, fakes)
    with pytest.raises(CutoverError, match="공개 도메인 경로=aws"):
        cut.run()
    assert cut.job.stage == ABORTED


def test_precheck_accepts_domain_already_on_source(state_dir, fakes):
    fakes["traffic"].target = "onprem"
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    assert _build(fakes["config"], state_dir, fakes, approve="ops-1").run() == COMPLETED
    assert fakes["traffic"].target == "aws"


def test_switch_failure_is_needs_recovery_after_point_of_no_return(state_dir, fakes):
    _simulate_replication(fakes)
    fakes["traffic"].fail_switch = True
    cut = _build(fakes["config"], state_dir, fakes, approve="ops-1")
    with pytest.raises(Exception):
        cut.run()
    assert cut.job.stage == "NEEDS_RECOVERY" and cut.job.checkpoint["failed_stage"] == SWITCHING
    assert fakes["db"].app_insert["aws"] is True and fakes["traffic"].target is None


def _revert_after(traffic: FakeTraffic, status_calls: int) -> None:
    orig_switch, orig_status = traffic.switch, traffic.status
    seen = {"n": None}

    def switch(target):
        seen["n"] = 0
        return orig_switch(target)

    def status():
        if seen["n"] is not None:
            seen["n"] += 1
            if seen["n"] > status_calls:
                return {"target": "onprem"}
        return orig_status()

    traffic.switch, traffic.status = switch, status


def test_switching_fails_if_route_did_not_move(state_dir, fakes):
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    _revert_after(fakes["traffic"], 0)
    cut = _build(fakes["config"], state_dir, fakes, approve="ops-1")
    with pytest.raises(CutoverError, match="공개 도메인 경로가 aws 가 아니다"):
        cut.run()
    assert cut.job.checkpoint["failed_stage"] == SWITCHING


def test_verifying_fails_if_domain_moved_back(state_dir, fakes):
    _simulate_replication(fakes)
    _grow_target_commits(fakes)
    _revert_after(fakes["traffic"], 1)
    cut = _build(fakes["config"], state_dir, fakes, approve="ops-1")
    with pytest.raises(CutoverError, match="공개 도메인이 대상으로 가고 있지 않다"):
        cut.run()
    assert cut.job.checkpoint["failed_stage"] == VERIFYING


def test_resumed_switch_does_not_call_again_when_already_moved(state_dir, fakes):
    cut = _build(fakes["config"], state_dir, fakes)
    fakes["traffic"].target = "aws"
    assert cut._switch_traffic() == {"already": True, "target": "aws"}
    assert "traffic:aws" not in fakes["ingress"].calls


def test_public_route_cannot_replace_required_ingress(state_dir, tmp_path):
    config = _config(with_ingress=False)
    fakes = {"bot": FakeBot(config.job.source), "connect": FakeConnect(), "db": FakeDb(),
             "verify": FakeVerify(), "traffic": FakeTraffic()}
    cut = _build(config, state_dir, fakes, approve="ops-1")
    with pytest.raises(CutoverError, match="접수 큐 제어 설정"):
        cut.run()
    assert fakes["traffic"].calls == []



@pytest.mark.parametrize(
    "traffic, match",
    [
        ({"app": "sample-app"}, "traffic.targets 매핑"),
        ({"app": "sample-app", "targets": {"gcp": "gcp"}}, "targets 에 없다"),
        ({"app": "Sample App", "targets": {"aws": "aws"}}, "traffic.app"),
        ({"app": "sample-app", "targets": {"aws": "AWS!"}}, "traffic.targets.aws"),
        ({"app": "sample-app", "targets": {"aws": "aws"}, "url": "x"}, "알 수 없는 키"),
    ],
)
def test_traffic_config_is_validated(traffic, match):
    raw = copy.deepcopy(RAW_CONFIG)
    raw["traffic"] = traffic
    with pytest.raises(ConfigError, match=match):
        parse_config(raw)


def test_traffic_is_off_by_default():
    assert not parse_config(copy.deepcopy(RAW_CONFIG)).traffic.enabled


def test_client_needs_platform_url_and_token():
    config = _config().traffic
    with pytest.raises(TrafficClientError, match="ONEPORT_PLATFORM_URL"):
        HttpTrafficClient(config, env={"ONEPORT_INGEST_TOKEN": "t"})
    client = HttpTrafficClient(config, env={"ONEPORT_PLATFORM_URL": "https://platform.example/",
                                            "ONEPORT_INGEST_TOKEN": "t"})
    assert client.url == "https://platform.example/api/v1/ingest/traffic/sample-app"
