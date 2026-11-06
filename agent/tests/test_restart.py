"""RolloutRestarter: restartAt 패치, 새 파드 Ready 대기, 멈춘 옛 파드 처리, 시간 초과."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent.config import RestartSettings
from agent.restart import RestartError, RolloutRestarter

NOW = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
OLD, NEW = "2026-11-07T11:00:00Z", "2026-11-07T12:00:05Z"


def pod(name: str, created: str, ready: bool) -> dict:
    return {"metadata": {"name": name, "creationTimestamp": created},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


class Api:
    def __init__(self, frames: list[list[dict]]) -> None:
        self.frames, self.patches, self.deleted, self.gets = frames, [], [], 0

    def patch(self, path: str, body: dict) -> dict:
        self.patches.append((path, body))
        return {"spec": {"replicas": 2}}

    def get(self, path: str) -> dict:
        frame = self.frames[min(self.gets, len(self.frames) - 1)]
        self.gets += 1
        return {"items": frame}

    def delete(self, path: str) -> None:
        self.deleted.append(path.rsplit("/", 1)[-1])


def restarter(api: Api, **overrides) -> RolloutRestarter:
    ticks = iter(range(0, 100000, 10))
    settings = RestartSettings(**{"timeout": 100, "stuck_after": 30, **overrides})
    return RolloutRestarter(settings, api, sleep=lambda _: None, clock=lambda: float(next(ticks)), now=lambda: NOW)


def test_patches_restart_at_and_waits_for_ready() -> None:
    api = Api([[pod("a", OLD, True), pod("b", OLD, True)], [pod("c", NEW, False), pod("b", OLD, True)],
               [pod("c", NEW, True), pod("d", NEW, True)]])
    restarter(api).restart_and_wait()
    path, body = api.patches[0]
    assert path.endswith("/namespaces/hackathon/rollouts/sample-app")
    assert body == {"spec": {"restartAt": "2026-11-07T12:00:00Z"}}
    assert api.deleted == []


def test_stuck_not_ready_old_pods_are_deleted_only_after_threshold() -> None:
    stuck = [pod("bad-1", OLD, False), pod("bad-2", OLD, False)]
    api = Api([stuck, stuck, stuck, stuck, [pod("c", NEW, True), pod("d", NEW, True)]])
    restarter(api).restart_and_wait()
    assert set(api.deleted) == {"bad-1", "bad-2"}


def test_ready_old_pods_are_never_deleted() -> None:
    api = Api([[pod("ok", OLD, True), pod("bad", OLD, False)]])
    with pytest.raises(RestartError):
        restarter(api, timeout=60).restart_and_wait()
    assert set(api.deleted) == {"bad"}


def test_timeout_when_new_pods_never_ready() -> None:
    api = Api([[pod("c", NEW, False), pod("d", NEW, False)]])
    with pytest.raises(RestartError, match="Ready"):
        restarter(api, timeout=30).restart_and_wait()


def test_terminating_pods_are_ignored() -> None:
    terminating = pod("old", OLD, False)
    terminating["metadata"]["deletionTimestamp"] = "2026-11-07T12:00:01Z"
    api = Api([[terminating, pod("c", NEW, True), pod("d", NEW, True)]])
    restarter(api).restart_and_wait()
    assert api.deleted == []
