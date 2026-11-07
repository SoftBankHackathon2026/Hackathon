from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Mapping

from .runs import NOTHING_TO_FIX, Run, RunStore

log = logging.getLogger("agent.platform")
AGENT_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
QUIET_STATES = frozenset({NOTHING_TO_FIX})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class PlatformClient:
    def __init__(self, base_url: str, token: str, agent_id: str, *, timeout: float = 5.0, opener: Any = None) -> None:
        url = urllib.parse.urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError("platform.url 형식이 틀렸다")
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise ValueError("platform.agent_id 형식이 틀렸다")
        self.base_url, self.agent_id = base_url.rstrip("/"), agent_id
        self._token, self._timeout = token, timeout
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._pushed: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"PlatformClient({self.base_url!r}, agent_id={self.agent_id!r})"

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Any:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": "application/json", "Accept": "application/json",
        })
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read(1024 * 1024)
        except urllib.error.HTTPError as exc:
            exc.close()
            log.warning("플랫폼 %s %s: HTTP %s", method, path.split("?")[0], exc.code)
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("플랫폼 %s %s: %s", method, path.split("?")[0], type(exc).__name__)
            return None
        return json.loads(raw) if raw else True

    def heartbeat(
        self, *, version: str, auto_apply: bool, interval_seconds: int, config: Mapping[str, Any] | None = None,
    ) -> None:
        body: dict[str, Any] = {
            "agent_id": self.agent_id, "version": version, "auto_apply": auto_apply,
            "interval_seconds": interval_seconds,
            "observed_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        if config is not None:
            body["config"] = dict(config)
        self._call("POST", "/api/v1/ingest/agent/heartbeat", body)

    def commands(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"agent_id": self.agent_id})
        found = self._call("GET", f"/api/v1/ingest/agent/commands?{query}")
        return [c for c in found if isinstance(c, dict)] if isinstance(found, list) else []

    def ack_command(self, command_id: int, result: str) -> None:
        self._call("POST", f"/api/v1/ingest/agent/commands/{int(command_id)}/ack", {"result": result[:200]})

    def push_run(self, run: Run, events: list[Mapping[str, str]]) -> bool:
        body = {**asdict(run), "agent_id": self.agent_id, "events": [dict(e) for e in events[-200:]]}
        body.pop("run_id")
        ok = self._call("PUT", f"/api/v1/ingest/agent/runs/{urllib.parse.quote(run.run_id)}", body) is not None
        if ok:
            self._pushed[run.run_id] = _mark(run, events)
        return ok

    def sync(self, runs: RunStore, limit: int = 20) -> int:
        sent = 0
        for run in reversed(runs.recent(limit, exclude=QUIET_STATES)):
            events = runs.events(run.run_id)
            if self._pushed.get(run.run_id) != _mark(run, events) and self.push_run(run, events):
                sent += 1
        return sent

    def approvals(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"agent_id": self.agent_id})
        found = self._call("GET", f"/api/v1/ingest/agent/approvals?{query}")
        return [a for a in found if isinstance(a, dict)] if isinstance(found, list) else []

    def ack(self, approval_id: int, result_state: str) -> None:
        self._call("POST", f"/api/v1/ingest/agent/approvals/{int(approval_id)}/ack", {"result_state": result_state[:32]})


def _mark(run: Run, events: list[Mapping[str, str]]) -> str:
    return f"{run.updated_at}|{run.state}|{len(events)}"


def handle_commands(client: PlatformClient, runtime: Any) -> bool:
    rerun = False
    for command in client.commands():
        kind = command.get("kind")
        by = str(command.get("requested_by") or "플랫폼")[:64]
        if kind == "settings" and runtime is not None:
            try:
                result = runtime.apply(command.get("payload") or {})
                log.info("플랫폼 설정 변경 (%s): %s", by, result)
            except ValueError as exc:
                result = f"거절함: {exc}"
        elif kind == "rerun":
            rerun = True
            result = "바로 다시 판정함"
        else:
            result = "모르는 명령이라 무시함"
        client.ack_command(int(command["id"]), result)
    return rerun


def handle_approvals(client: PlatformClient, remediator: Any, runs: RunStore) -> list[tuple[str, str]]:
    handled: list[tuple[str, str]] = []
    for approval in client.approvals():
        run_id, decision = str(approval.get("run_id", "")), approval.get("decision")
        by = str(approval.get("requested_by") or "플랫폼")[:64]
        try:
            if decision == "approve":
                state = remediator.approve(run_id, by=f"{by}, 플랫폼").state
            elif decision == "reject":
                state = remediator.reject(run_id, by=f"{by}, 플랫폼").state
            else:
                continue
        except ValueError:
            current = runs.get(run_id)
            state = current.state if current else "UNKNOWN"
        client.ack(int(approval["id"]), state)
        handled.append((run_id, state))
    return handled
