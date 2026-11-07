from __future__ import annotations

from types import SimpleNamespace

from agent.config import RestartSettings
from agent.demo_fault import WRONG_HOST, inject, restore
from agent.fakes import GOOD_HOST, FakeKv


def cfg():
    return SimpleNamespace(restart=RestartSettings(), target_db={"host": GOOD_HOST, "port": 5432})


def test_inject_writes_wrong_host_and_deletes_every_pod() -> None:
    kv = FakeKv({"DB_HOST": GOOD_HOST, "DB_PORT": "5432"})
    deleted: list[str] = []

    class Api:
        def get(self, path):
            assert "labelSelector=app%3Dsample-app" in path
            return {"items": [{"metadata": {"name": "a"}}, {"metadata": {"name": "b"}}]}

        def delete(self, path):
            deleted.append(path.rsplit("/", 1)[-1])

    inject(cfg(), kv, Api())
    assert kv.data["DB_HOST"] == WRONG_HOST and kv.version == 2 and deleted == ["a", "b"]


def test_restore_writes_plan_values_and_restarts() -> None:
    kv = FakeKv({"DB_HOST": WRONG_HOST, "DB_PORT": "1"})
    restarts: list[int] = []
    restore(cfg(), kv, SimpleNamespace(restart_and_wait=lambda: restarts.append(1)))
    assert kv.data == {"DB_HOST": GOOD_HOST, "DB_PORT": "5432"} and restarts == [1]


def test_inject_can_leave_old_source_endpoint() -> None:
    kv = FakeKv({"DB_HOST": GOOD_HOST, "DB_PORT": "5432"})

    class Api:
        def get(self, path):
            return {"items": []}

    inject(cfg(), kv, Api(), host="old-source.example.rds.amazonaws.com")
    assert kv.data["DB_HOST"] == "old-source.example.rds.amazonaws.com" and kv.data["DB_PORT"] == "5432"
