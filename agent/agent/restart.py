"""sample-app 재시작. Argo Rollouts 의 restartAt 을 쓰고 새 파드가 전부 Ready 가 될 때까지 기다린다.

rollout restart 는 가용 파드 수를 지키며 한 개씩 지운다. 모든 파드가 NotReady 면 더 지우지 못하고 멈출 수 있어
(장애 주입 직후가 그렇다), stuck_after 가 지나도록 남아 있는 옛 NotReady 파드만 직접 지운다.
그런 파드는 트래픽을 받지 않으므로 지워도 가용량이 줄지 않는다. Ready 인 옛 파드는 건드리지 않는다.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .config import RestartSettings

log = logging.getLogger("agent.restart")

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
API_SERVER = "https://kubernetes.default.svc"


class RestartError(RuntimeError):
    """재시작 요청 실패 또는 Ready 대기 시간 초과."""


class Restarter(Protocol):
    def restart_and_wait(self) -> None: ...


class KubeApi(Protocol):
    def get(self, path: str) -> dict[str, Any]: ...

    def patch(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]: ...

    def delete(self, path: str) -> None: ...


class InClusterKubeApi:
    """파드의 ServiceAccount 로 API 서버를 부른다. kubectl 을 이미지에 넣지 않으려고 직접 호출한다."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._token = (SA_DIR / "token").read_text(encoding="utf-8").strip()
        self._ctx = ssl.create_default_context(cafile=str(SA_DIR / "ca.crt"))
        self._timeout = timeout

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None, content_type: str) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(API_SERVER + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": content_type, "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ctx) as resp:  # noqa: S310 - 고정 주소
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RestartError(f"쿠버네티스 API {method} {path}: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RestartError(f"쿠버네티스 API {method} {path}: {type(exc).__name__}") from None

    def get(self, path: str) -> dict[str, Any]:
        return self._call("GET", path, None, "application/json")

    def patch(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("PATCH", path, body, "application/merge-patch+json")

    def delete(self, path: str) -> None:
        self._call("DELETE", path, None, "application/json")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _is_ready(pod: Mapping[str, Any]) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in pod.get("status", {}).get("conditions", [])
    )


class RolloutRestarter:
    def __init__(
        self,
        settings: RestartSettings,
        api: KubeApi,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings, self.api = settings, api
        self.sleep, self.clock, self.now = sleep, clock, now
        ns = urllib.parse.quote(settings.namespace)
        self._rollout = f"/apis/argoproj.io/v1alpha1/namespaces/{ns}/rollouts/{urllib.parse.quote(settings.rollout)}"
        self._pods = f"/api/v1/namespaces/{ns}/pods"

    def restart_and_wait(self) -> None:
        restart_at = self.now().replace(microsecond=0)
        stamp = restart_at.isoformat().replace("+00:00", "Z")
        rollout = self.api.patch(self._rollout, {"spec": {"restartAt": stamp}})
        replicas = int(rollout.get("spec", {}).get("replicas") or 1)
        log.info("rollout %s restartAt=%s replicas=%d", self.settings.rollout, stamp, replicas)

        started = self.clock()
        deadline = started + self.settings.timeout
        while True:
            selector = urllib.parse.quote(self.settings.selector)
            pods = self.api.get(f"{self._pods}?labelSelector={selector}").get("items", [])
            live = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
            old = [p for p in live if _parse_time(p["metadata"]["creationTimestamp"]) < restart_at]
            ready_new = [p for p in live if p not in old and _is_ready(p)]
            if not old and len(ready_new) >= replicas:
                log.info("새 파드 %d개 Ready", len(ready_new))
                return
            if self.clock() >= deadline:
                raise RestartError(
                    f"{self.settings.timeout:.0f}초 안에 Ready 가 되지 않았다 (새 Ready {len(ready_new)}/{replicas}, 옛 파드 {len(old)})"
                )
            if self.clock() - started >= self.settings.stuck_after:
                for pod in old:
                    if not _is_ready(pod):
                        name = pod["metadata"]["name"]
                        log.warning("옛 NotReady 파드를 지운다: %s", name)
                        self.api.delete(f"{self._pods}/{urllib.parse.quote(name)}")
            self.sleep(self.settings.poll_interval)
