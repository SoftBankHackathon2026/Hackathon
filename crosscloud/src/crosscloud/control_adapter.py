"""원가연 앱 제어 API 6개를 durable-ingress 제어 API 로 옮기는 어댑터.

migration.cloud 는 config.cloud.yaml 의 application.control_url 로 이 어댑터를
부른다(계약: migration/CLOUD_MIGRATION.md「앱 제어 API 계약」, 클라이언트:
migration/worker_client.py). 어댑터는 플랫폼을 바꾸지 않는다. 쓰기는 접수
큐(durable-ingress)가 앱으로 전달하는 것을 멈추고 다시 여는 것으로 다룬다
(설계 0.1 S15).

| 앱 제어 API        | 접수 큐 제어 API                                       |
|--------------------|--------------------------------------------------------|
| GET /status        | GET /status → state·target·known_targets·in_flight     |
| POST /pause        | POST /pause (settled:true 확인)                        |
| POST /drain        | GET /status 를 paused·in_flight 0 이 될 때까지 되풀이  |
| POST /target       | 대상 앱 /readyz 확인 → POST /target (expected_epoch)   |
| POST /resume       | POST /resume (expected_epoch). 대상 라벨일 때만        |
| GET /healthz       | 대상 앱 /readyz 연속 N 회(25초 안)                     |

접수 큐의 대상 이름은 클라우드 라벨(aws·gcp)과 같다. 응답이 모호하거나 접수
큐 호출이 실패하면 성공으로 답하지 않는다(502·409·503). 127.0.0.1 에서만
듣는다.

  config = control_adapter.load_adapter_config(cloud_yaml, ingress_env)
  adapter = control_adapter.ControlAdapter(config, ingress)
  control_adapter.make_server(adapter, config.port).serve_forever()
"""

import collections.abc
import dataclasses
import http.server
import json
import pathlib
import threading
import time
import urllib.parse
from typing import Any

import yaml

from crosscloud import health_probe, ingress_control, model

Reply = tuple[int, dict[str, Any]]

_LISTEN_HOST = "127.0.0.1"
_READYZ_PATH = "/readyz"
_READYZ_TIMEOUT_SECONDS = 5.0
_READYZ_INTERVAL_SECONDS = 1.0
_DRAIN_POLL_SECONDS = 0.5
_MAX_BODY_BYTES = 65536


@dataclasses.dataclass(frozen=True)
class AdapterConfig:
    """어댑터 설정. 값은 config.cloud.yaml·ingress.env 에서 읽는다.

    Attributes:
        source: 원본 라벨(job.source).
        target: 대상 라벨(job.target).
        port: 들을 포트(application.control_url 의 포트).
        source_app_url: 원본 앱 주소(INGRESS_TARGETS[source].url).
        target_app_url: 대상 앱 주소(INGRESS_TARGETS[target].url).
        readiness_successes: /healthz 가 요구하는 연속 성공 수.
        readiness_deadline_seconds: 연속 성공을 기다리는 상한. 원가연 HTTP
            한도(30초)보다 짧아야 한다.
    """

    source: model.Cloud
    target: model.Cloud
    port: int
    source_app_url: str
    target_app_url: str
    readiness_successes: int = 3
    readiness_deadline_seconds: float = 25.0

    def app_url(self, label: str) -> str | None:
        """라벨의 앱 주소. 이 job 의 라벨이 아니면 None."""
        return {
            self.source: self.source_app_url,
            self.target: self.target_app_url,
        }.get(label)


class ControlAdapter:
    """앱 제어 API 한 벌. 메서드는 (HTTP 코드, JSON 본문)을 돌려준다."""

    def __init__(
        self,
        config: AdapterConfig,
        ingress: ingress_control.IngressControl,
        *,
        check_ready: collections.abc.Callable[
            [str, float], bool
        ] = health_probe.is_healthy,
        clock: collections.abc.Callable[[], float] = time.monotonic,
        sleep: collections.abc.Callable[[float], None] = time.sleep,
    ) -> None:
        """설정과 접수 큐 클라이언트를 받는다."""
        self._config = config
        self._ingress = ingress
        self._check_ready = check_ready
        self._clock = clock
        self._sleep = sleep
        # 바꾸는 호출(pause·target·resume)은 한 번에 하나만.
        self._changing = threading.Lock()

    def status(self) -> Reply:
        """GET /status."""
        return self._guard(lambda: (200, self._app_status(self._read())))

    def pause(self) -> Reply:
        """POST /pause — 새 전달을 멈춘다. 접수(202)는 계속된다."""
        return self._changed(lambda: (200, self._app_status(self._pause())))

    def drain(self, payload: dict[str, Any]) -> Reply:
        """POST /drain {"timeout": 초} — paused·in_flight 0 을 기다린다."""
        timeout = payload.get("timeout")
        if type(timeout) not in (int, float) or not 0 < timeout <= 600:
            return 400, {"error": "timeout 은 0 초과 600 이하 숫자"}
        return self._guard(lambda: self._drain(float(timeout)))

    def target(self, payload: dict[str, Any]) -> Reply:
        """POST /target {"target": 라벨} — 대상 앱이 준비됐으면 큐를 돌린다."""
        label = payload.get("target")
        if label != self._config.target:
            return 409, {"error": f"대상 라벨({self._config.target})만 허용"}
        return self._changed(lambda: self._switch(label))

    def resume(self) -> Reply:
        """POST /resume — 큐가 대상 라벨을 가리킬 때만 전달을 재개한다."""
        return self._changed(self._resume)

    def healthz(self) -> Reply:
        """GET /healthz — 큐가 가리키는 앱의 /readyz 연속 N 회."""
        return self._guard(self._healthz)

    def start_on_source(self) -> Reply:
        """전환 전 준비: 새로 뜬 큐를 원본 라벨로 돌리고 전달을 연다.

        durable-ingress 는 처음에 'onprem' 대상·paused 로 뜬다. 대상 라벨로
        이미 돌았으면(전환 뒤) 원본으로 되돌리지 않고 409 를 낸다.
        """
        return self._changed(self._start_on_source)

    def _read(self) -> ingress_control.IngressState:
        return self._ingress.status()

    def _pause(self) -> ingress_control.IngressState:
        self._ingress.pause()
        state = self._read()
        if state.mode != "paused":
            raise ingress_control.IngressError("pause 뒤에도 paused 가 아니다")
        return state

    def _drain(self, timeout: float) -> Reply:
        deadline = self._clock() + timeout
        while True:
            state = self._read()
            settled = state.mode == "paused" and state.in_flight == 0
            if settled or self._clock() >= deadline:
                body = {"settled": settled, **self._app_status(state)}
                return (200 if settled else 409), body
            self._sleep(_DRAIN_POLL_SECONDS)

    def _switch(self, label: str) -> Reply:
        state = self._read()
        if state.target == label:
            return 200, self._app_status(state)
        if state.mode != "paused" or state.in_flight:
            return 409, {"error": "큐가 paused·in_flight 0 이 아니다"}
        url = self._config.target_app_url + _READYZ_PATH
        if not self._check_ready(url, _READYZ_TIMEOUT_SECONDS):
            return 503, {"error": f"대상 앱 {_READYZ_PATH} 준비 안 됨"}
        self._ingress.set_target(label, state.epoch)
        switched = self._read()
        if switched.target != label or switched.mode != "paused":
            raise ingress_control.IngressError("target 변경이 반영되지 않았다")
        return 200, self._app_status(switched)

    def _resume(self) -> Reply:
        state = self._read()
        if state.target != self._config.target:
            return 409, {
                "error": f"큐가 {state.target} 을 가리킨다 — 대상"
                f" {self._config.target} 에서만 재개한다"
            }
        if state.mode == "paused":
            self._ingress.resume(state.epoch)
            state = self._read()
        if state.mode != "running":
            raise ingress_control.IngressError(
                "resume 뒤에도 running 이 아니다"
            )
        return 200, self._app_status(state)

    def _healthz(self) -> Reply:
        label = self._label(self._read())
        url = self._config.app_url(label) + _READYZ_PATH
        streak = health_probe.consecutive_successes(
            lambda: self._check_ready(url, _READYZ_TIMEOUT_SECONDS),
            needed=self._config.readiness_successes,
            deadline_seconds=self._config.readiness_deadline_seconds,
            interval=_READYZ_INTERVAL_SECONDS,
            clock=self._clock,
            sleep=self._sleep,
        )
        healthy = streak >= self._config.readiness_successes
        return (200 if healthy else 503), {"healthy": healthy, "target": label}

    def _start_on_source(self) -> Reply:
        state = self._read()
        if state.target == self._config.target:
            return 409, {
                "error": "이미 대상으로 전환됐다 — 원본으로 돌리지 않는다"
            }
        if state.target != self._config.source:
            if state.mode != "paused":
                state = self._pause()
            self._ingress.set_target(self._config.source, state.epoch)
            state = self._read()
        if state.mode == "paused":
            self._ingress.resume(state.epoch)
            state = self._read()
        return 200, self._app_status(state)

    def _app_status(
        self, state: ingress_control.IngressState
    ) -> dict[str, Any]:
        return {
            "state": "RUNNING" if state.mode == "running" else "PAUSED",
            "target": self._label(state),
            "known_targets": [self._config.source, self._config.target],
            "in_flight": state.in_flight,
        }

    def _label(self, state: ingress_control.IngressState) -> str:
        if self._config.app_url(state.target) is None:
            raise ingress_control.IngressError(
                f"큐 대상 {state.target} 은 이 job 의 라벨이 아니다"
            )
        return state.target

    def _changed(self, action: collections.abc.Callable[[], Reply]) -> Reply:
        if not self._changing.acquire(blocking=False):
            return 409, {"error": "다른 제어 호출이 진행 중이다"}
        try:
            return self._guard(action)
        finally:
            self._changing.release()

    @staticmethod
    def _guard(action: collections.abc.Callable[[], Reply]) -> Reply:
        try:
            return action()
        except ingress_control.IngressError as error:
            return 502, {"error": str(error)}


def load_adapter_config(
    cloud_config: pathlib.Path, ingress_env: pathlib.Path
) -> AdapterConfig:
    """인계 파일 둘(config.cloud.yaml·ingress.env)에서 어댑터 설정을 읽는다.

    Raises:
        ValueError: 라벨·주소·포트가 없거나 형식이 틀릴 때.
    """
    document = yaml.safe_load(cloud_config.read_text())
    try:
        job = document["job"]
        control_url = document["application"]["control_url"]
    except (KeyError, TypeError) as error:
        raise ValueError(f"config.cloud.yaml 에 {error} 가 없다") from None
    if {job.get("source"), job.get("target")} != {"aws", "gcp"}:
        raise ValueError("job.source·target 은 aws·gcp 여야 한다")
    port = urllib.parse.urlsplit(control_url).port
    if port is None:
        raise ValueError("application.control_url 에 포트가 없다")
    targets = json.loads(_env_file(ingress_env).get("INGRESS_TARGETS", "{}"))
    urls = {}
    for label in (job["source"], job["target"]):
        entry = targets.get(label)
        if not isinstance(entry, dict) or not entry.get("url"):
            raise ValueError(f"INGRESS_TARGETS 에 {label} 주소가 없다")
        urls[label] = entry["url"].rstrip("/")
    return AdapterConfig(
        source=job["source"],
        target=job["target"],
        port=port,
        source_app_url=urls[job["source"]],
        target_app_url=urls[job["target"]],
    )


def make_server(
    adapter: ControlAdapter, port: int
) -> http.server.ThreadingHTTPServer:
    """127.0.0.1:port 에 앱 제어 API 서버를 만든다. 실행은 부르는 쪽이 한다."""
    routes = {
        ("GET", "/status"): lambda body: adapter.status(),
        ("GET", "/healthz"): lambda body: adapter.healthz(),
        ("POST", "/pause"): lambda body: adapter.pause(),
        ("POST", "/drain"): adapter.drain,
        ("POST", "/target"): adapter.target,
        ("POST", "/resume"): lambda body: adapter.resume(),
    }

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - 표준 핸들러 이름
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - 표준 핸들러 이름
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def _dispatch(self, method: str) -> None:
            route = routes.get((method, self.path))
            if route is None:
                self._send(404, {"error": "not found"})
                return
            body = self._body() if method == "POST" else {}
            if body is None:
                self._send(400, {"error": "본문은 JSON 객체여야 한다"})
                return
            self._send(*route(body))

        def _body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > _MAX_BODY_BYTES:
                return None
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return None
            return body if isinstance(body, dict) else None

        def _send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return http.server.ThreadingHTTPServer((_LISTEN_HOST, port), Handler)


def _env_file(path: pathlib.Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values
