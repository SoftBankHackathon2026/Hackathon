"""시험용 durable-ingress 제어 API(127.0.0.1).

sample-app src/durable-ingress/service.js 의 제어 계약을 흉내 낸다: /status 는
mode·target·epoch·in_flight·last_error, /pause 는 settled:true, /target 은
paused·in_flight 0·등록 대상·epoch 일치일 때만 epoch+1, /resume 은 epoch 일치.
토큰이 있으면 Bearer 가 맞아야 한다(401 CONTROL_UNAUTHORIZED).
"""

import contextlib
import http.server
import json
import threading
import time


class FakeIngress:
    def __init__(self, targets=("onprem", "aws", "gcp"), token=None):
        self.targets = set(targets)
        self.token = token
        self.state = {
            "mode": "paused",
            "target": "onprem",
            "epoch": "0",
            "in_flight": 0,
            "last_error": None,
        }
        self.calls = []
        self.status_override = None
        self.delays = {}

    def handle(self, method, path, body, authorization):
        self.calls.append((method, path, body))
        if self.token and authorization != f"Bearer {self.token}":
            return 401, {"error": "CONTROL_UNAUTHORIZED"}
        if method == "GET" and path == "/status":
            if self.status_override is not None:
                return self.status_override
            return 200, dict(self.state)
        if method != "POST":
            return 404, {"error": "not found"}
        if path == "/pause":
            self.state["mode"] = "paused"
            return 200, {**self.state, "settled": True}
        if path == "/target":
            return self._target(body)
        if path == "/resume":
            if body.get("expected_epoch") != self.state["epoch"]:
                return 409, {"error": {"code": "EPOCH_CONFLICT"}}
            self.state["mode"] = "running"
            return 200, dict(self.state)
        return 404, {"error": "not found"}

    def _target(self, body):
        if self.state["mode"] != "paused" or self.state["in_flight"]:
            return 409, {"error": {"code": "PAUSE_REQUIRED"}}
        if body.get("target") not in self.targets:
            return 409, {"error": {"code": "TARGET_NOT_CONFIGURED"}}
        if body.get("expected_epoch") != self.state["epoch"]:
            return 409, {"error": {"code": "EPOCH_CONFLICT"}}
        self.state["target"] = body["target"]
        self.state["epoch"] = str(int(self.state["epoch"]) + 1)
        return 200, dict(self.state)

    @contextlib.contextmanager
    def serve(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, method):
                time.sleep(fake.delays.get(self.path, 0))
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                code, payload = fake.handle(
                    method, self.path, body, self.headers.get("Authorization")
                )
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._reply("GET")

            def do_POST(self):  # noqa: N802
                self._reply("POST")

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
