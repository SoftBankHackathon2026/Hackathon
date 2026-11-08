"""Full-service handoff safety gates; no live infrastructure is touched."""
import copy

import pytest

from migration.config import ConfigError, parse_config
from tests.conftest import RAW_CONFIG

FULL = {
    "destination_control_url": "https://receipt.example/control",
    "destination_token_env": "CLOUD_CONTROL_TOKEN",
    "destination_target": "aws_eks",
    "destination_app_version": "fixture-v1",
    "forward_target": "aws_receipt",
    "destination_receipt_url": "https://app.example",
    "destination_api_token_env": "CLOUD_API_TOKEN",
}


def full_raw():
    raw = copy.deepcopy(RAW_CONFIG)
    raw["full_migration"] = copy.deepcopy(FULL)
    raw["ingress"]["token_env"] = "INGRESS_CONTROL_TOKEN"
    raw["ingress"]["targets"]["aws"] = "aws_eks"
    raw["ingress"]["app_checks"]["aws"]["url"] = "https://aws-app.example"
    return raw


def test_full_service_config_keeps_dns_cutover_separate():
    raw = full_raw()
    assert parse_config(raw).full_migration.destination_target == "aws_eks"
    raw["traffic"] = {"app": "sample-app", "targets": {"onprem": "onprem", "aws": "aws"}}
    with pytest.raises(ConfigError, match="traffic"):
        parse_config(raw)


@pytest.mark.parametrize("field,value", [
    ("destination_control_url", "http://receipt.example/control"),
    ("destination_control_url", "https://user:secret@receipt.example"),
    ("destination_receipt_url", "https://app.example?token=secret"),
    ("destination_receipt_url", "https://app.example/path"),
    ("destination_token_env", ""),
    ("timeout_seconds", 0),
])
def test_full_service_rejects_unsafe_or_incomplete_config(field, value):
    raw = full_raw()
    raw["full_migration"][field] = value
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_full_service_rejects_queue_app_target_mismatch():
    raw = full_raw()
    raw["ingress"]["targets"]["aws"] = "aws_db"
    with pytest.raises(ConfigError):
        parse_config(raw)

from migration.cutover import Cutover, CutoverError, Ports, COMPLETED, NEEDS_RECOVERY, ABORTED
from migration.state import CutoverStore
from migration.full_migration import HandoffError, ReceiptHandoff
from tests.test_cutover import FakeBot, FakeConnect, FakeDb, FakeIngress, FakeVerify, _simulate_replication
from tests.test_cutover_traffic import FakeTraffic


class FakeHandoff:
    def __init__(self, source, calls):
        self.source, self.calls = source, calls
        self.active = False
        self.public_ok = True
        self.fail_activate_response = False
        self.fail_transfer = False

    def precheck(self):
        return {"source_ledger_id": "source", "destination_ledger_id": "cloud"}

    def seal(self, binding):
        self.calls.append("seal")
        self.source.mode = "paused"
        return {"id": "forward_001", **binding, "manifest": {"count": 2, "digest": "abc"}}

    def transfer(self, sealed):
        self.calls.append("import")
        if self.fail_transfer:
            raise HandoffError("import failed")
        return {"count": 2, "digest": "abc"}

    def activate(self, sealed):
        self.calls.append("activate")
        self.active = True
        if self.fail_activate_response:
            raise HandoffError("activation response lost")
        return self.resume_destination(sealed)

    def resume_destination(self, sealed):
        assert self.active
        return {"ledger_id": "cloud", "mode": "running", "target": "aws_eks"}

    def forward(self, sealed):
        assert self.active
        self.calls.append("forward")
        return {"ledger_id": "source", "admission": "forwarding"}

    def verify_destination(self, sealed):
        self.calls.append("destination-proof")
        if not self.public_ok:
            raise HandoffError("destination receipt is still onprem")
        return {"receipt_ledger_id": "cloud", "app_environment": "aws"}


def full_cut(state_dir, objects=None, raw=None):
    cfg = parse_config(raw or full_raw())
    if objects is None:
        objects = {"bot": FakeBot(), "connect": FakeConnect(), "db": FakeDb(),
                   "ingress": FakeIngress(), "verify": FakeVerify()}
        calls = objects["ingress"].calls
        objects["handoff"] = FakeHandoff(objects["ingress"], calls)
        _simulate_replication(objects)
        db = objects["db"]
        def deliver(count):
            if objects["handoff"].active:
                assert objects["ingress"].mode == "paused"
                assert db.app_insert["aws"] and not db.app_insert["onprem"]
                db.rows["aws"] += count
            else:
                assert objects["ingress"].mode == "running"
                db.rows["onprem"] += count
        objects["bot"].on_batch = deliver
        revoke, grant = db.revoke_app_insert, db.grant_app_insert
        db.revoke_app_insert = lambda t: (calls.append("revoke"), revoke(t))[1]
        db.grant_app_insert = lambda t: (calls.append("grant"), grant(t))[1]
    ticks = [0.0]
    def clock():
        ticks[0] += 1
        return ticks[0]
    docs = {k: {"name": getattr(cfg.job, k + "_connector"), "config": {}} for k in ("source", "sink")}
    cut = Cutover(cfg, CutoverStore(state_dir), Ports(**objects), connector_docs=docs,
                  approve="test-approved", clock=clock, sleep=lambda _: None,
                  policy_gate=lambda stage, checks: {"plan_digest": "test", "execution_digest": "test"})
    return cut, objects


def test_full_service_transfers_ledger_and_uses_cloud_app_while_source_stays_paused(state_dir):
    cut, objects = full_cut(state_dir)
    assert cut.run() == COMPLETED
    calls = objects["ingress"].calls
    order = ["seal", "revoke", "import", "grant", "activate", "forward", "destination-proof"]
    assert [calls.index(item) for item in order] == sorted(calls.index(item) for item in order)
    assert objects["ingress"].mode == "paused" and objects["ingress"].target == "onprem"
    assert objects["bot"].generation == "stopped"
    assert cut.job.checkpoint["post_switch"]["ingress"]["ledger_id"] == "cloud"


def test_full_service_public_route_failure_cannot_be_marked_completed(state_dir):
    cut, objects = full_cut(state_dir)
    objects["handoff"].public_ok = False
    with pytest.raises(CutoverError, match="destination receipt"):
        cut.run()
    assert cut.job.stage == NEEDS_RECOVERY
    assert objects["bot"].generation == "stopped"
    assert not objects["db"].app_insert["onprem"]
    objects["handoff"].public_ok = True
    resumed, _ = full_cut(state_dir, objects)
    assert resumed.run(force_retry=True) == COMPLETED
    assert objects["ingress"].calls.count("import") == 1


def test_full_service_activation_response_loss_never_reopens_source(state_dir):
    cut, objects = full_cut(state_dir)
    objects["handoff"].fail_activate_response = True
    with pytest.raises(CutoverError, match="activation response lost"):
        cut.run()
    assert cut.job.stage == NEEDS_RECOVERY
    assert cut.job.checkpoint["receipt_activation_attempted"] is True
    assert objects["handoff"].active and not objects["db"].app_insert["onprem"]
    objects["handoff"].fail_activate_response = False
    resumed, _ = full_cut(state_dir, objects)
    assert resumed.run(force_retry=True) == COMPLETED


def test_full_service_import_failure_before_grant_keeps_source_sealed(state_dir):
    cut, objects = full_cut(state_dir)
    objects["handoff"].fail_transfer = True
    with pytest.raises(CutoverError):
        cut.run()
    assert not objects["handoff"].active
    assert not any(objects["db"].app_insert.values())
    assert objects["ingress"].mode == "paused"
    assert "grant" not in objects["ingress"].calls


def test_full_service_cannot_resume_as_database_only(state_dir):
    cut, objects = full_cut(state_dir)
    cut._save()
    with pytest.raises(ConfigError, match="모드"):
        full_cut(state_dir, objects, raw=copy.deepcopy(RAW_CONFIG))


@pytest.mark.parametrize("change", ["missing", "environment", "database"])
def test_full_service_requires_target_app_identity_before_start(change):
    raw = full_raw()
    if change == "missing":
        del raw["ingress"]["app_checks"]["aws"]
    else:
        raw["ingress"]["app_checks"]["aws"][change] = ""
    with pytest.raises(ConfigError, match="identity"):
        parse_config(raw)


def test_full_service_config_change_cannot_redirect_a_resumed_handoff(state_dir):
    cut, objects = full_cut(state_dir)
    cut._save()
    raw = full_raw()
    raw["full_migration"]["forward_target"] = "another_queue"
    with pytest.raises(ConfigError, match="설정이 변경"):
        full_cut(state_dir, objects, raw=raw)


def test_seal_response_loss_disallows_automatic_rollback(state_dir):
    cut, objects = full_cut(state_dir)
    def lost(binding):
        objects["ingress"].mode = "paused"
        raise HandoffError("seal response lost")
    objects["handoff"].seal = lost
    with pytest.raises(CutoverError, match="seal response lost"):
        cut.run()
    assert cut.job.stage == ABORTED
    assert cut.job.checkpoint["receipt_seal_attempted"]
    with pytest.raises(CutoverError, match="봉인"):
        cut.abort_rollback()
    assert objects["ingress"].mode == "paused"


def handoff_client():
    config = parse_config(full_raw())
    return ReceiptHandoff(config, env={"INGRESS_CONTROL_TOKEN": "source-token",
                                      "CLOUD_CONTROL_TOKEN": "destination-token",
                                      "CLOUD_API_TOKEN": "api-token"})


SEALED = {"id": "forward_001", "source_ledger_id": "source", "destination_ledger_id": "cloud",
          "manifest": {"count": 1, "digest": "abc", "last_cursor": "1-0"}}


def test_handoff_streams_pages_without_returning_records_to_checkpoints():
    client = handoff_client()
    body = {**SEALED, "cursor": "0-0", "next_cursor": "1-0", "done": True,
            "records": [{"body": "private-payload"}]}
    calls = []
    def call(side, method, path, data=None):
        calls.append((side, method, path, data))
        if side == "source":
            return body
        if method == "POST":
            return {"ok": True}
        return {"ledger_id": "cloud", "handoff_id": "forward_001",
                "imported_count": 1, "imported_digest": "abc"}
    client._call = call
    summary = client.transfer(SEALED)
    assert summary == {"count": 1, "digest": "abc"}
    assert calls[1][3] == body
    assert "private-payload" not in str(summary)


@pytest.mark.parametrize("bad", ["binding", "count", "no_progress", "digest"])
def test_handoff_transfer_rejects_mixed_or_incomplete_records(bad):
    client = handoff_client()
    page = {**SEALED, "cursor": "0-0", "next_cursor": "1-0", "done": True, "records": [{}]}
    if bad == "binding":
        page["destination_ledger_id"] = "unrelated"
    if bad == "count":
        page["records"] = [{}, {}]
    if bad == "no_progress":
        page.update(records=[], done=False, next_cursor="0-0")
    def call(side, method, path, data=None):
        if side == "source":
            return page
        return {"ledger_id": "cloud", "handoff_id": "forward_001", "imported_count": 1,
                "imported_digest": "wrong" if bad == "digest" else "abc"}
    client._call = call
    with pytest.raises(HandoffError):
        client.transfer(SEALED)


def test_destination_restart_resumes_only_the_bound_active_ledger():
    client = handoff_client()
    state = {"mode": "paused", "epoch": "7", "target": "aws_eks"}
    calls = []
    def call(side, method, path, data=None):
        calls.append((method, path, data))
        if path == "/handoff/status":
            return {"ledger_id": "cloud", "handoff_id": "forward_001", "state": "active", "admission": "open"}
        if path == "/resume":
            assert data == {"expected_epoch": "7"}
            state["mode"] = "running"
        return state
    client._call = call
    assert client.resume_destination(SEALED)["mode"] == "running"
    assert ("POST", "/resume", {"expected_epoch": "7"}) in calls


def test_destination_identity_does_not_accept_source_forwarder():
    client = handoff_client()
    client._call = lambda *args: {"ledger_id": "source", "handoff_id": "forward_001", "admission": "forwarding"}
    with pytest.raises(HandoffError, match="unrelated"):
        client.verify_destination(SEALED)


def test_app_version_mismatch_is_rejected_before_activating_cloud_queue():
    client = handoff_client()
    client._call = lambda *args: {"environment": "aws", "database": {"name": "migration_demo"},
                                  "version": "old-image", "writes_paused": False}
    with pytest.raises(HandoffError, match="artifact"):
        client._check_app_identity(writes_open=False)


def test_app_identity_precheck_does_not_require_target_write_readiness():
    client = handoff_client()
    paths = []
    def call(side, method, path, data=None):
        paths.append(path)
        return {"environment": "aws", "database": {"name": "migration_demo"},
                "version": "fixture-v1", "writes_paused": True}
    client._call = call
    client._check_app_identity(writes_open=False)
    assert paths == ["/api/info"]
    with pytest.raises(HandoffError, match="ready"):
        client._check_app_identity(writes_open=True)
