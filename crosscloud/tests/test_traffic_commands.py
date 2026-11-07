import fcntl
import json

import http_fixture
import pytest
from botocore import exceptions

from crosscloud.traffic import commands, gate, route53

_JOB = "ccx_aws_gcp_20261107153000"
_PARAMS = gate.GateParams(
    health_consecutive_successes=3,
    max_probe_error_rate=0.05,
    health_deadline_seconds=25,
    error_window_seconds=30,
)


class _FakeRoute:
    zone_id = "Z0PRIVATE"
    record_name = "app.ccx.internal."

    def __init__(self, gcp_weight=0, insync=True, set_error=None):
        self.weights = {"aws": 100 - gcp_weight, "gcp": gcp_weight}
        self.insync = insync
        self.set_error = set_error
        self.changes = []

    def read(self):
        return route53.RouteSnapshot(dict(self.weights), 30)

    def set_weights(self, weights):
        if self.set_error:
            raise self.set_error
        self.weights = dict(weights)
        self.changes.append(dict(weights))
        return f"/change/C{len(self.changes)}"

    def change_insync(self, change_id):
        return self.insync

    def wait_insync(self, change_id, **_):
        return self.insync


class _FakeSignals:
    def __init__(self, health=3, error_rate=0.0):
        self.health = health
        self.error_rate = error_rate
        self.measured = 0

    def target_health_successes(self):
        self.measured += 1
        return self.health

    def probe_error_rate(self):
        return self.error_rate


def _save_stage(migration_module, state_dir, stage, **checkpoint):
    state = migration_module("migration.state")
    store = state.CutoverStore(state_dir)
    try:
        store.save(
            state.CutoverJob(
                _JOB, "aws->gcp", "aws", "gcp", stage, checkpoint, ""
            )
        )
    finally:
        store.close()


def _commands(
    tmp_path, route=None, signals=None, adapter_state=lambda: "RUNNING"
):
    state_dir = tmp_path / "migration"
    state_dir.mkdir(exist_ok=True)
    return commands.TrafficCommands(
        route=route or _FakeRoute(),
        change_record=route53.ChangeRecord(tmp_path / "traffic.json"),
        source="aws",
        target="gcp",
        migration_state_dir=state_dir,
        job_id=_JOB,
        read_adapter_state=adapter_state,
        signals=signals or _FakeSignals(),
        gate_params=_PARAMS,
        insync_timeout_seconds=120,
    )


def test_shift_steps_through_weights_with_gate_each_time(tmp_path):
    route = _FakeRoute()
    signals = _FakeSignals()
    traffic = _commands(tmp_path, route, signals)

    results = traffic.shift((10, 50, 100))

    assert [r.outcome for r in results] == ["applied"] * 3
    assert route.changes == [
        {"gcp": 10, "aws": 90},
        {"gcp": 50, "aws": 50},
        {"gcp": 100, "aws": 0},
    ]
    assert signals.measured == 3
    assert results[-1].status.target_weight == 100
    assert results[-1].status.last_change_id == "/change/C3"
    assert results[-1].status.last_change_insync is True


def test_shift_stops_at_first_gate_denial(tmp_path):
    route = _FakeRoute()
    traffic = _commands(tmp_path, route, _FakeSignals(error_rate=None))

    results = traffic.shift((10, 50))

    assert len(results) == 1
    assert results[0].outcome == "rejected"
    assert "오류율" in results[0].reason
    assert route.changes == []


def test_shift_reports_unknown_when_change_not_insync(tmp_path):
    route = _FakeRoute(insync=False)
    traffic = _commands(tmp_path, route)

    results = traffic.shift((10, 50))

    assert [r.outcome for r in results] == ["unknown"]
    assert "routing-status" in results[0].reason
    assert json.loads((tmp_path / "traffic.json").read_text()) == {
        "last_change_id": "/change/C1"
    }


def test_shift_without_change_is_applied_and_skips_gate(tmp_path):
    signals = _FakeSignals(health=None)
    traffic = _commands(tmp_path, _FakeRoute(gcp_weight=50), signals)

    results = traffic.shift((50,))

    assert results[0].outcome == "applied"
    assert signals.measured == 0


def test_shift_down_needs_no_gate(tmp_path):
    route = _FakeRoute(gcp_weight=50)
    traffic = _commands(tmp_path, route, _FakeSignals(health=None))

    results = traffic.shift((10,))

    assert results[0].outcome == "applied"
    assert route.weights == {"gcp": 10, "aws": 90}


@pytest.mark.parametrize("weights", [(), (101,), (-1,), (10, 1.5)])
def test_shift_rejects_invalid_weights(tmp_path, weights):
    with pytest.raises(ValueError):
        _commands(tmp_path).shift(weights)


def test_restore_puts_source_back_to_100_without_gate(tmp_path):
    route = _FakeRoute(gcp_weight=100)
    traffic = _commands(tmp_path, route, _FakeSignals(health=None))

    result = traffic.restore()

    assert result.outcome == "applied"
    assert route.weights == {"gcp": 0, "aws": 100}
    assert result.status.source_weight == 100


@pytest.mark.parametrize("stage", ["FENCING", "SWITCHING", "COMPLETED"])
def test_commands_refused_from_fencing_on(tmp_path, migration_module, stage):
    route = _FakeRoute(gcp_weight=100)
    traffic = _commands(tmp_path, route)
    _save_stage(migration_module, tmp_path / "migration", stage)

    restored = traffic.restore()
    shifted = traffic.shift((50,))

    assert restored.outcome == "rejected"
    assert shifted[0].outcome == "rejected"
    assert stage in restored.reason
    assert route.changes == []


def test_restore_allowed_when_fencing_failed_before_pause(
    tmp_path, migration_module
):
    route = _FakeRoute(gcp_weight=50)
    traffic = _commands(tmp_path, route, adapter_state=lambda: "RUNNING")
    _save_stage(
        migration_module,
        tmp_path / "migration",
        "FAILED",
        failed_stage="FENCING",
    )

    assert traffic.restore().outcome == "applied"


def test_restore_refused_when_fencing_failed_after_pause(
    tmp_path, migration_module
):
    route = _FakeRoute(gcp_weight=100)
    traffic = _commands(tmp_path, route, adapter_state=lambda: "PAUSED")
    _save_stage(
        migration_module,
        tmp_path / "migration",
        "FAILED",
        failed_stage="FENCING",
    )

    assert traffic.restore().outcome == "rejected"
    assert route.changes == []


def test_commands_refused_while_migration_cloud_runs(tmp_path):
    route = _FakeRoute()
    traffic = _commands(tmp_path, route)
    with (tmp_path / "migration" / "cloud.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        result = traffic.restore()

    assert result.outcome == "rejected"
    assert "cloud.lock" in result.reason
    assert route.changes == []


def test_commands_refused_when_status_unreadable(tmp_path):
    route = _FakeRoute()
    traffic = _commands(tmp_path, route)
    (tmp_path / "migration" / "cutover.sqlite3").write_text("broken")

    result = traffic.shift((10,))[0]

    assert result.outcome == "rejected"
    assert route.changes == []


def test_route53_client_error_is_rejected(tmp_path):
    error = exceptions.ClientError(
        {"Error": {"Code": "InvalidChangeBatch", "Message": "x"}},
        "ChangeResourceRecordSets",
    )
    traffic = _commands(tmp_path, _FakeRoute(set_error=error))

    result = traffic.shift((10,))[0]

    assert result.outcome == "rejected"
    assert "InvalidChangeBatch" in result.reason


def test_route53_connection_error_is_unknown(tmp_path):
    error = exceptions.EndpointConnectionError(endpoint_url="https://r53")
    traffic = _commands(tmp_path, _FakeRoute(set_error=error))

    result = traffic.shift((10,))[0]

    assert result.outcome == "unknown"


def test_status_reports_weights_stage_and_last_change(
    tmp_path, migration_module
):
    traffic = _commands(tmp_path, _FakeRoute(gcp_weight=10))
    _save_stage(migration_module, tmp_path / "migration", "READY")
    route53.ChangeRecord(tmp_path / "traffic.json").save("/change/C7")

    status = traffic.status()

    assert status == commands.RoutingStatus(
        zone_id="Z0PRIVATE",
        record_name="app.ccx.internal.",
        source_weight=90,
        target_weight=10,
        ttl_seconds=30,
        last_change_id="/change/C7",
        last_change_insync=True,
        stage="READY",
        failed_stage=None,
    )


def test_status_shows_unknown_stage_when_unreadable(tmp_path):
    traffic = _commands(tmp_path)
    (tmp_path / "migration" / "cutover.sqlite3").write_text("broken")

    status = traffic.status()

    assert status.stage is None
    assert status.last_change_id is None
    assert status.last_change_insync is None


def test_read_adapter_state_from_control_url():
    body = json.dumps({"state": "PAUSED", "target": "aws"}).encode()
    with http_fixture.serve({"/status": (200, body)}) as url:
        assert commands.read_adapter_state(url) == "PAUSED"


def test_read_adapter_state_none_when_unreachable_or_malformed():
    assert commands.read_adapter_state("http://127.0.0.1:9") is None
    with http_fixture.serve({"/status": (200, b"[1]")}) as url:
        assert commands.read_adapter_state(url) is None
