from copy import deepcopy
from decimal import Decimal

import pytest

from migration.cloud.plan import connectors, parse, snapshot_complete
from migration.cloud.postgres import canonical
from migration.cloud.runner import Runner
from migration.config import ConfigError
from migration.cutover import ApprovalRequired, CutoverError
from migration.state import CutoverStore


def raw_plan():
    return {
        "targets": {
            "aws": {"host": "a", "dbname": "shop"},
            "gcp": {"host": "b", "dbname": "shop"},
        },
        "job": {"job_id": "cloud_001", "source": "aws", "target": "gcp"},
        "application": {
            "control_url": "http://adapter",
            "tables": [
                {"name": "public.orders", "primary_key": ["tenant", "id"]},
                {"name": "public.empty", "primary_key": ["id"]},
            ],
        },
        "cutover": {"stage_timeout_seconds": 0.02, "poll_interval_seconds": 0.01},
    }


def test_plan_and_native_delete_connectors():
    docs = connectors(parse(raw_plan()))
    assert len(docs) == 3
    source = docs["cloud_001-source"]
    assert source["table.include.list"] == "public\\.empty,public\\.orders"
    assert source["database.password"] == "${env:MIGRATION_CDC_PASSWORD}"
    sink = docs["cloud_001-sink-1"]
    assert (
        sink["primary.key.fields"] == "tenant,id" and sink["delete.enabled"] == "true"
    )
    assert not any(k.startswith("transforms") for k in sink)
    assert sink["table.name.format"] == "public.orders"
    reverse = raw_plan()
    reverse["job"].update(source="gcp", target="aws")
    assert connectors(parse(reverse))["cloud_001-source"]["database.hostname"] == "b"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["application"].update(tables=[]),
        lambda r: r["application"]["tables"][0].update(primary_key=[]),
        lambda r: r["application"]["tables"][0].update(name="public.orders;DROP"),
        lambda r: r["application"]["tables"].append(r["application"]["tables"][0]),
        lambda r: r["job"].update(job_id="UPPER"),
        lambda r: r["targets"].update(gcp=deepcopy(r["targets"]["aws"])),
    ],
)
def test_reject_invalid_manifest(mutation):
    raw = raw_plan()
    mutation(raw)
    with pytest.raises(ConfigError):
        parse(raw)


@pytest.mark.parametrize(
    "offset,expected",
    [
        ({}, False),
        ({"snapshot": True, "lsn": 123}, False),
        ({"snapshot": "INITIAL", "lsn": 123}, False),
        ({"snapshot": "INITIAL", "last_snapshot_record": True, "lsn": 123}, True),
        ({"snapshot_completed": True, "lsn": 123}, True),
        ({"lsn": 123}, True),
        ({"snapshot": False, "lsn": 123}, True),
    ],
)
def test_snapshot_offset_contract(offset, expected):
    doc = {"offsets": [{"partition": {"server": "cloud_001"}, "offset": offset}]}
    assert snapshot_complete(doc, "cloud_001") is expected
    assert not snapshot_complete(doc, "different") and not snapshot_complete(
        None, "cloud_001"
    )


def test_canonical_precision_and_types():
    a = {"n": Decimal("123456789012345678901234567890.123456789"), "x": None}
    b = {"x": None, "n": Decimal("123456789012345678901234567890.123456788")}
    assert canonical(a) != canonical(b)
    assert canonical(Decimal("1.00")) == canonical(Decimal("1"))
    assert canonical("1") != canonical(Decimal("1"))


class App:
    def __init__(self):
        self.target = "aws"
        self.state = "RUNNING"
        self.events = []

    def status(self):
        return dict(
            target=self.target,
            state=self.state,
            known_targets=["aws", "gcp"],
            in_flight=0,
        )

    def pause(self):
        self.state = "PAUSED"
        self.events.append("pause")

    def drain(self, timeout):
        return {"settled": True}

    def switch_target(self, target):
        self.target = target
        self.events.append("switch")

    def resume(self):
        self.state = "RUNNING"
        self.events.append("resume")

    def healthz(self):
        return {"healthy": True, "target": self.target}


class Db:
    def __init__(self):
        self.fences = set()
        self.reports = []
        self.verify_pass = True

    def precheck(self):
        return {}

    def fence(self, target, **kw):
        self.fences.add(target)

    def assert_fenced(self, target):
        assert target in self.fences

    def prepare(self):
        pass

    def verify(self):
        self.reports.append(1)
        return {"passed": self.verify_pass, "tables": {}}

    def sync_sequences(self):
        assert self.fences == {"aws", "gcp"}

    def release_target(self):
        self.fences.remove("gcp")


class Connect:
    def __init__(self):
        self.docs = {}
        self.stopped = set()

    def exists(self, n):
        return n in self.docs

    def get_config(self, n):
        return self.docs[n]

    def put_config(self, n, d):
        self.docs[n] = d

    def offsets(self, n):
        return {
            "offsets": [{"partition": {"server": "cloud_001"}, "offset": {"lsn": 123}}]
        }

    def is_running(self, n):
        return (n in self.docs and n not in self.stopped, {})

    def stop(self, n):
        self.stopped.add(n)

    def status(self, n):
        return {"connector": {"state": "STOPPED"}, "tasks": []}

    def delete(self, n):
        del self.docs[n]


@pytest.fixture
def setup(tmp_path):
    store = CutoverStore(tmp_path)
    db, con, app = Db(), Connect(), App()
    yield Runner(parse(raw_plan()), store, db, con, app), db, con, app, store
    store.close()


def test_ready_resume_and_success(setup):
    r, db, con, app, store = setup
    with pytest.raises(ApprovalRequired):
        r.run()
    assert (
        store.load("cloud_001").stage == "READY"
        and app.events == []
        and db.fences == {"gcp"}
    )
    r = Runner(r.plan, store, db, con, app, approve="reviewed")
    assert r.run().stage == "COMPLETED" and db.fences == {"aws"} and not con.docs
    assert len(db.reports) == 2 and app.events == ["pause", "switch", "resume"]


def test_mismatch_never_switches_and_retains_fences(setup):
    r, db, con, app, store = setup
    r.approve = "reviewed"
    db.verify_pass = False
    with pytest.raises(CutoverError):
        r.run()
    assert store.load("cloud_001").stage == "FAILED"
    assert db.fences == {"aws", "gcp"} and app.target == "aws" and app.state == "PAUSED"


def test_health_failure_requires_manual_recovery(setup):
    r, db, con, app, store = setup
    r.approve = "reviewed"
    app.healthz = lambda: {"healthy": False}
    with pytest.raises(CutoverError):
        r.run()
    assert store.load("cloud_001").stage == "NEEDS_RECOVERY"
    with pytest.raises(CutoverError):
        r.run(retry=True)
    assert db.fences == {"aws"}


def test_config_drift_blocks_resume(setup):
    r, db, con, app, store = setup
    raw = raw_plan()
    raw["application"]["tables"][0]["primary_key"] = ["id"]
    with pytest.raises(ConfigError):
        Runner(parse(raw), store, db, con, app)
