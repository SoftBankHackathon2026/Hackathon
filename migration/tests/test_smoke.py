"""스모크 모듈 단위 테스트. 요청 API 계약대로 답하는 가짜 HTTP 서버 두 대(원본·대상)를 띄워 실제 HTTP 로 돌린다."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

import pytest

from migration import smoke
from migration.smoke import ERROR, FAILED, NOT_RUN, OK, SKIPPED, UNKNOWN, SmokeConfigError, run_smoke

TOKEN = "s3cret-Token-for-smoke-tests-0123456789"
JOB = "forward_001"
Override = Callable[[str, str, Any], "tuple[int, Any, dict[str, str]] | None"]


def api_error(code: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": "m"}}


class FakeApp:
    """sample-app 요청 API. override 가 값을 돌려주면 그 응답을 그대로 쓴다."""

    def __init__(self, environment: str, token: str | None = None) -> None:
        self.environment = environment
        self.token = token
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.auth_headers: list[str | None] = []
        self.replica: FakeApp | None = None
        self.replicate_after_gets = 0  # 대상 GET 이 이 횟수를 넘어야 행이 보인다
        self.pending: list[dict[str, Any]] = []
        self.override: Override | None = None
        self.server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def posts(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] == "POST"]

    def respond(self, method: str, path: str, headers: Any, body: Any) -> tuple[int, Any, dict[str, str]]:
        self.calls.append((method, path))
        self.auth_headers.append(headers.get("Authorization"))
        if self.override is not None:
            forced = self.override(method, path, body)
            if forced is not None:
                return forced
        if self.token and headers.get("Authorization") != f"Bearer {self.token}":
            return 401, api_error("UNAUTHORIZED"), {}
        if method == "POST":
            return self._post(body)
        return self._get(path.rsplit("/", 1)[-1])

    def _meta(self) -> dict[str, Any]:
        return {"status": "committed", "database": {"name": "migration_demo"}, "environment": self.environment,
                "region": "test"}

    def _post(self, body: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
        existing = self.rows.get(body["request_id"])
        if existing is not None:
            if existing != body:
                return 409, api_error("REQUEST_CONFLICT"), {}
            return 200, {**self._meta(), "created": False, "request": existing}, {}
        self.rows[body["request_id"]] = body
        if self.replica is not None:
            self.replica.pending.append(body)
        return 201, {**self._meta(), "created": True, "request": body}, {}

    def _get(self, rid: str) -> tuple[int, Any, dict[str, str]]:
        if self.pending and self.replicate_after_gets <= 0:
            for row in self.pending:
                self.rows[row["request_id"]] = row
            self.pending = []
        self.replicate_after_gets -= 1
        row = self.rows.get(rid)
        if row is None:
            return 404, api_error("REQUEST_NOT_FOUND"), {}
        return 200, {**self._meta(), "request": row}, {}


def _handler(app: FakeApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            code, payload, headers = app.respond(self.command, self.path, self.headers, body)
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = _serve

        def log_message(self, *args: Any) -> None:
            pass

    return Handler


@pytest.fixture
def apps() -> Iterator[tuple[FakeApp, FakeApp]]:
    source, target = FakeApp("onprem"), FakeApp("aws")
    source.replica = target
    for app in (source, target):
        app.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(app))
        app.server.daemon_threads = True
        threading.Thread(target=app.server.serve_forever, args=(0.01,), daemon=True).start()
    yield source, target
    for app in (source, target):
        assert app.server is not None
        app.server.shutdown()
        app.server.server_close()


def pre(source: FakeApp, target: FakeApp, **kwargs: Any) -> smoke.SmokeResult:
    options: dict[str, Any] = {"replication_timeout": 0.3, "poll_interval": 0.01, "http_timeout": 1.0}
    options.update(kwargs)
    return run_smoke("pre_cutover", job_id=JOB, source_url=source.url, target_url=target.url, **options)


def final(source: FakeApp, target: FakeApp, request_id: str | None, **kwargs: Any) -> smoke.SmokeResult:
    return run_smoke("final_verify", job_id=JOB, source_url=source.url, target_url=target.url,
                     request_id=request_id, http_timeout=1.0, **kwargs)


def only(method: str, code: int, payload: Any, app_path_contains: str = "", headers: dict[str, str] | None = None) -> Override:
    def override(m: str, path: str, _body: Any) -> tuple[int, Any, dict[str, str]] | None:
        if m == method and app_path_contains in path:
            return code, payload, headers or {}
        return None
    return override


def nth_post(n: int, code: int, payload: Any) -> Override:
    """n 번째 POST 만 응답을 바꾼다."""
    seen = {"count": 0}

    def override(method: str, _path: str, _body: Any) -> tuple[int, Any, dict[str, str]] | None:
        if method != "POST":
            return None
        seen["count"] += 1
        return (code, payload, {}) if seen["count"] == n else None
    return override


def results(result: smoke.SmokeResult) -> dict[str, str]:
    return {name: step.result for name, step in result.steps.items()}


# ---- pre_cutover 정상 경로와 대상 쓰기 금지 -----------------------------------------


def test_pre_cutover_pass(apps):
    source, target = apps
    result = pre(source, target)
    assert results(result) == dict.fromkeys(smoke.STEPS, OK)
    assert (result.status, result.db_connected, result.order_query_ok, result.exit_code) == ("pass", True, True, 0)
    check = result.to_check()
    assert set(check) == {"status", "observed_at", "source", "evidence_id", "detail"}
    assert check["source"] == "migration.smoke"
    steps = check["detail"]["steps"]
    assert (steps["create"]["http_code"], steps["retry"]["http_code"]) == (201, 200)
    assert (steps["read_back"]["http_code"], steps["replicated"]["http_code"]) == (200, 200)


def test_pre_cutover_never_posts_to_target(apps):
    source, target = apps
    pre(source, target)
    assert target.posts() == []
    assert len(source.posts()) == 2  # create + retry


def test_smoke_row_shape_is_identifiable_and_accepted_by_verify(apps):
    source, target = apps
    result = pre(source, target)
    row = source.rows[result.request_id]
    assert set(row) == set(smoke.RECORD_FIELDS)
    assert row["run_id"] == "smoke_forward_001"
    assert row["phase"] == "smoke"
    assert int(row["request_no"]) > 0  # verify.normalize_row 가 int 로 읽는다
    assert row["created_at"].endswith("Z") and len(row["created_at"].split(".")[1]) == 4  # 밀리초
    assert target.rows[result.request_id] == row


def test_replication_seen_after_polling(apps):
    source, target = apps
    target.replicate_after_gets = 3  # 사전 확인 1회 + 폴링 2회 뒤에 보인다
    result = pre(source, target)
    assert result.steps["replicated"].result == OK
    assert result.steps["replicated"].attempts >= 3


# ---- 대상 사전 확인: 대상을 못 믿으면 원본에 쓰지 않는다 ----------------------------


@pytest.mark.parametrize("code,payload,expected_status,expected_db", [
    (503, api_error("DB_UNAVAILABLE"), "pass", False),       # 앱이 DB 연결 불가라고 답했다: 확정 실패
    (401, api_error("UNAUTHORIZED"), "unknown", False),
    (503, api_error("API_AUTH_UNCONFIGURED"), "unknown", False),
    (404, b"<html>ingress</html>", "error", False),          # 앱이 아닌 404 는 조회 증거가 아니다
    (502, b"bad gateway", "error", False),
])
def test_target_probe_failure_creates_no_source_row(apps, code, payload, expected_status, expected_db):
    source, target = apps
    target.override = only("GET", code, payload)
    result = pre(source, target)
    assert source.posts() == []
    assert source.rows == {}
    assert result.status == expected_status
    assert result.db_connected is expected_db
    assert result.order_query_ok is False
    assert results(result) == dict.fromkeys(smoke.STEPS, NOT_RUN)


def test_target_unreachable_is_error_and_creates_no_source_row(apps):
    source, target = apps
    result = run_smoke("pre_cutover", job_id=JOB, source_url=source.url, target_url="http://127.0.0.1:9",
                       http_timeout=0.5, replication_timeout=0)
    assert result.status == "error"
    assert source.rows == {}


# ---- 스텝별 응답 코드 ---------------------------------------------------------------


@pytest.mark.parametrize("code,payload,step,expected,status", [
    (409, api_error("REQUEST_CONFLICT"), "create", FAILED, "pass"),
    (400, api_error("INVALID_REQUEST"), "create", FAILED, "pass"),
    (500, api_error("CONTEXT_INVALID"), "create", FAILED, "pass"),
    (401, api_error("UNAUTHORIZED"), "create", UNKNOWN, "unknown"),
    (503, api_error("WRITES_PAUSED"), "create", UNKNOWN, "unknown"),
    (503, api_error("DB_UNAVAILABLE"), "create", UNKNOWN, "unknown"),  # 원본 장애로 대상을 판정하지 않는다
    (502, b"bad gateway", "create", ERROR, "error"),
])
def test_create_rejection(apps, code, payload, step, expected, status):
    source, target = apps
    source.override = only("POST", code, payload)
    result = pre(source, target)
    assert result.steps[step].result == expected
    assert result.steps[step].http_code == code
    assert result.status == status
    assert result.order_query_ok is False
    assert result.db_connected is True  # 대상 사전 확인은 성공했다
    assert results(result)["retry"] == NOT_RUN
    assert results(result)["replicated"] == NOT_RUN


def test_writes_paused_is_not_marked_commit_uncertain_but_db_error_is(apps):
    source, target = apps
    source.override = only("POST", 503, api_error("WRITES_PAUSED"), headers={"Retry-After": "1"})
    assert "commit_uncertain" not in pre(source, target).steps["create"].reason
    source.override = only("POST", 401, api_error("UNAUTHORIZED"))
    assert "commit_uncertain" not in pre(source, target).steps["create"].reason
    source.override = only("POST", 503, api_error("DB_UNAVAILABLE"))
    assert "commit_uncertain" in pre(source, target).steps["create"].reason


def test_create_200_on_fresh_id_is_failed(apps):
    source, target = apps
    source.override = nth_post(1, 200, {"created": False, "request": {}, "environment": "onprem"})
    result = pre(source, target)
    assert result.steps["create"].result == FAILED
    assert result.status == "pass" and result.order_query_ok is False


def test_html_200_on_create_is_observation_error_not_app_failure(apps):
    source, target = apps
    source.override = only("POST", 200, b"<html>login</html>")
    result = pre(source, target)
    assert result.steps["create"].result == ERROR
    assert result.status == "error"


def test_retry_409_fails_order_check_but_still_observes_replication(apps):
    source, target = apps
    source.override = nth_post(2, 409, api_error("REQUEST_CONFLICT"))
    result = pre(source, target)
    assert results(result) == {"create": OK, "retry": FAILED, "read_back": OK, "replicated": OK}
    assert result.steps["retry"].http_code == 409
    assert (result.status, result.db_connected, result.order_query_ok, result.exit_code) == ("pass", True, False, 2)


def test_retry_that_reports_created_true_is_failed(apps):
    source, target = apps
    def override(method: str, _path: str, body: Any) -> Any:
        if method == "POST" and body["request_id"] in source.rows:
            return 200, {"created": True, "request": body, "environment": "onprem"}, {}
        return None
    source.override = override
    result = pre(source, target)
    assert result.steps["retry"].reason == "created_flag_mismatch"


@pytest.mark.parametrize("field,value", [("payload", "tampered"), ("request_no", "1"), ("created_at", "2000-01-01T00:00:00.000Z")])
def test_read_back_field_mismatch(apps, field, value):
    source, target = apps
    def override(method: str, path: str, _body: Any) -> Any:
        if method == "GET":
            rid = path.rsplit("/", 1)[-1]
            return 200, {"request": {**source.rows[rid], field: value}, "environment": "onprem"}, {}
        return None
    source.override = override
    result = pre(source, target)
    assert result.steps["read_back"].result == FAILED
    assert result.order_query_ok is False


def test_read_back_404_is_failed(apps):
    source, target = apps
    source.override = only("GET", 404, api_error("REQUEST_NOT_FOUND"))
    result = pre(source, target)
    assert result.steps["read_back"].result == FAILED
    assert result.steps["read_back"].http_code == 404


# ---- 복제 판정 ---------------------------------------------------------------------


def test_replication_timeout_is_unknown_not_failure(apps):
    source, target = apps
    target.replicate_after_gets = 10_000
    result = pre(source, target, replication_timeout=0.1)
    step = result.steps["replicated"]
    assert (step.result, step.http_code, step.reason) == (UNKNOWN, 404, "replication_not_seen_within_timeout")
    assert step.attempts >= 2
    assert (result.status, result.db_connected, result.order_query_ok, result.exit_code) == ("unknown", True, False, 3)


def test_replicated_record_differs(apps):
    source, target = apps
    def override(method: str, path: str, _body: Any) -> Any:
        rid = path.rsplit("/", 1)[-1]
        if method == "GET" and rid in source.rows:
            return 200, {"request": {**source.rows[rid], "payload": "drift"}, "environment": "aws"}, {}
        return None
    target.override = override
    result = pre(source, target)
    assert result.steps["replicated"].reason == "record_mismatch"
    assert result.status == "pass" and result.order_query_ok is False


def test_same_environment_is_not_replication_evidence(apps):
    source, target = apps
    target.environment = source.environment
    result = pre(source, target)
    assert result.steps["replicated"].reason == "source_and_target_same_environment"
    assert result.status == "unknown"


def test_missing_environment_is_not_replication_evidence(apps):
    source, target = apps
    def override(method: str, path: str, _body: Any) -> Any:
        rid = path.rsplit("/", 1)[-1]
        if method == "GET" and rid in source.rows:
            return 200, {"request": source.rows[rid]}, {}
        return None
    target.override = override
    result = pre(source, target)
    assert result.steps["replicated"].reason == "environment_missing"
    assert result.status == "unknown"


def test_target_db_outage_during_polling_is_db_not_connected(apps):
    source, target = apps
    seen = {"gets": 0}
    def override(method: str, _path: str, _body: Any) -> Any:
        seen["gets"] += 1
        return None if seen["gets"] == 1 else (503, api_error("DB_UNAVAILABLE"), {})
    target.override = override
    result = pre(source, target, replication_timeout=0.05)
    assert result.steps["replicated"].result == FAILED
    assert (result.status, result.db_connected, result.order_query_ok) == ("pass", False, False)


def test_slow_target_during_polling_is_error(apps):
    source, target = apps
    seen = {"gets": 0}
    def override(method: str, _path: str, _body: Any) -> Any:
        seen["gets"] += 1
        if seen["gets"] > 1:
            time.sleep(0.4)
        return None
    target.override = override
    target.replicate_after_gets = 10_000
    result = pre(source, target, http_timeout=0.05, replication_timeout=0.1)
    assert result.steps["replicated"].result == ERROR
    assert result.steps["replicated"].http_code is None
    assert (result.status, result.db_connected) == ("error", False)


def test_source_timeout_on_create_is_error_with_uncertain_commit(apps):
    source, target = apps
    def override(method: str, _path: str, _body: Any) -> Any:
        if method == "POST":
            time.sleep(0.4)
        return None
    source.override = override
    result = pre(source, target, http_timeout=0.05)
    step = result.steps["create"]
    assert (step.result, step.http_code) == (ERROR, None)
    assert "commit_uncertain" in step.reason
    assert result.status == "error"
    assert result.request_id  # 행이 남았을 수 있으니 ID 는 결과에 남긴다


def test_polling_stops_at_deadline(apps):
    source, target = apps
    target.replicate_after_gets = 10_000
    started = time.monotonic()
    pre(source, target, replication_timeout=0.2, poll_interval=0.05)
    assert time.monotonic() - started < 2


# ---- final_verify: GET 만 ---------------------------------------------------------


def test_final_verify_only_gets_and_passes(apps):
    source, target = apps
    rid = pre(source, target).request_id
    source.calls.clear()
    target.calls.clear()
    result = final(source, target, rid)
    assert source.posts() == [] and target.posts() == []
    assert len(source.calls) == 1 and len(target.calls) == 1
    assert results(result) == {"create": SKIPPED, "retry": SKIPPED, "read_back": OK, "replicated": OK}
    assert (result.status, result.db_connected, result.order_query_ok, result.exit_code) == ("pass", True, True, 0)


def test_final_verify_works_while_writes_are_paused(apps):
    source, target = apps
    rid = pre(source, target).request_id
    source.override = target.override = only("POST", 503, api_error("WRITES_PAUSED"))
    assert final(source, target, rid).status == "pass"


def test_final_verify_target_missing_row_is_definite_failure(apps):
    source, target = apps
    rid = pre(source, target).request_id
    del target.rows[rid]
    result = final(source, target, rid)
    assert result.steps["replicated"].reason == "source_row_missing_on_target"
    assert (result.status, result.db_connected, result.order_query_ok) == ("pass", True, False)


def test_final_verify_field_drift(apps):
    source, target = apps
    rid = pre(source, target).request_id
    target.rows[rid] = {**target.rows[rid], "payload": "drift"}
    result = final(source, target, rid)
    assert result.steps["replicated"].reason == "record_mismatch"
    assert result.order_query_ok is False


@pytest.mark.parametrize("request_id", [None, "", "not-a-uuid"])
def test_final_verify_without_reference_row_is_unknown(apps, request_id):
    source, target = apps
    result = final(source, target, request_id)
    assert result.steps["read_back"].reason == "request_id_missing_or_invalid"
    assert result.status == "unknown" and result.order_query_ok is False
    assert result.db_connected is True  # 대상 연결은 별도로 관측했다
    assert source.calls == []


def test_final_verify_reference_row_absent_on_source_is_unknown(apps):
    source, target = apps
    result = final(source, target, "21d10fd2-5b3c-4ab3-9c7f-c47ee6234e59")
    assert result.steps["read_back"].reason == "reference_row_not_on_source"
    assert result.status == "unknown"


def test_final_verify_rejects_row_of_other_job(apps):
    source, target = apps
    rid = "21d10fd2-5b3c-4ab3-9c7f-c47ee6234e59"
    row = {"request_id": rid, "run_id": "forward_001", "phase": "onprem_live", "request_no": "1",
           "payload": "p", "created_at": "2026-11-04T00:00:00.000Z"}
    source.rows[rid] = target.rows[rid] = row
    result = final(source, target, rid)
    assert result.steps["read_back"].reason == "not_a_smoke_row_of_this_job"
    assert result.status == "unknown"


def test_final_verify_target_timeout_is_error(apps):
    source, target = apps
    rid = pre(source, target).request_id
    target.override = lambda *_: (time.sleep(0.4), None)[1]
    result = run_smoke("final_verify", job_id=JOB, source_url=source.url, target_url=target.url,
                       request_id=rid, http_timeout=0.05)
    assert result.steps["replicated"].result == ERROR
    assert (result.status, result.db_connected) == ("error", False)


def test_final_verify_source_down_still_observes_target_but_never_passes(apps):
    source, target = apps
    rid = pre(source, target).request_id
    result = run_smoke("final_verify", job_id=JOB, source_url="http://127.0.0.1:9", target_url=target.url,
                       request_id=rid, http_timeout=0.5)
    assert result.steps["read_back"].result == ERROR
    assert result.db_connected is True
    assert result.status == "error" and result.order_query_ok is False


# ---- check 변환: error/unknown 을 pass 로 만들지 않는다 ------------------------------


def test_error_outranks_definite_failure(apps):
    source, target = apps
    source.override = nth_post(2, 409, api_error("REQUEST_CONFLICT"))
    seen = {"gets": 0}
    def override(method: str, _path: str, _body: Any) -> Any:
        seen["gets"] += 1
        return None if seen["gets"] == 1 else (502, b"bad gateway", {})
    target.override = override
    result = pre(source, target, replication_timeout=0)
    assert results(result)["retry"] == FAILED
    assert results(result)["replicated"] == ERROR
    assert result.status == "error"


@pytest.mark.parametrize("step_results,status", [
    ({"create": NOT_RUN}, "unknown"),  # 아무것도 관측하지 못한 결과는 통과가 아니다
    ({"replicated": UNKNOWN}, "unknown"),
    ({"replicated": ERROR}, "error"),
])
def test_status_never_passes_without_evidence(step_results, status):
    result = smoke.SmokeResult("pre_cutover", JOB, "smoke_x", None)
    for name in smoke.STEPS:
        result.steps[name].result = OK
    for name, value in step_results.items():
        result.steps[name].result = value
    result.db_connected = True
    assert result.status == status
    assert result.order_query_ok is False
    assert result.exit_code == 3


# ---- 인증과 토큰 비노출 --------------------------------------------------------------


def test_bearer_token_sent_when_enforced(apps):
    source, target = apps
    source.token = target.token = TOKEN
    result = pre(source, target, token=TOKEN)
    assert result.status == "pass" and result.order_query_ok is True
    assert set(source.auth_headers) == {f"Bearer {TOKEN}"}


def test_missing_token_on_enforced_app_is_unknown_not_pass(apps):
    source, target = apps
    source.token = target.token = TOKEN
    result = pre(source, target)
    assert result.status == "unknown"
    assert result.target_probe.http_code == 401
    assert source.rows == {}


def test_token_never_appears_in_results_or_logs(apps, caplog):
    source, target = apps
    source.token = target.token = TOKEN
    caplog.set_level("DEBUG")
    outputs = [pre(source, target, token=TOKEN)]
    rid = outputs[0].request_id
    outputs.append(final(source, target, rid, token=TOKEN))
    outputs.append(pre(source, target, token=TOKEN + "x"))  # 틀린 토큰: 401
    # 앱이 오류 응답에 헤더를 그대로 되돌려 줘도 결과로 옮기지 않는다.
    source.override = only("POST", 400, {"error": {"code": f"Bearer {TOKEN}", "message": TOKEN}})
    outputs.append(pre(source, target, token=TOKEN))
    source.override = None
    outputs.append(run_smoke("pre_cutover", job_id=JOB, source_url="http://127.0.0.1:9", target_url="http://127.0.0.1:9",
                             token=TOKEN, http_timeout=0.2, replication_timeout=0))
    text = json.dumps([r.to_check() for r in outputs]) + repr(outputs) + caplog.text
    assert TOKEN not in text
    assert "Bearer" not in text


def test_redirect_is_not_followed_with_token(apps):
    source, target = apps
    target.token = TOKEN
    source.override = only("POST", 302, b"", headers={"Location": f"{target.url}/api/requests"})
    result = pre(source, target, token=TOKEN)
    assert result.steps["create"].result == ERROR
    assert target.posts() == []


def test_proxy_environment_is_ignored(apps, monkeypatch):
    source, target = apps
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    assert pre(source, target).status == "pass"


def test_token_refused_over_cleartext_non_loopback():
    with pytest.raises(SmokeConfigError) as err:
        run_smoke("pre_cutover", job_id=JOB, source_url="http://sample-app:8080", target_url="https://t", token=TOKEN)
    assert TOKEN not in str(err.value)


@pytest.mark.parametrize("url", [
    "http://sample-app.hackathon.svc",
    "http://sample-app.hackathon.svc:8080",
    "http://sample-app.hackathon.svc.cluster.local",
    "http://sample-app.hackathon.svc.cluster.local:8080/",
    "http://SAMPLE-APP.Hackathon.SVC.Cluster.Local:8080",
    "http://127.0.0.1:8080",
    "http://[::1]:8080",
    "https://sample-app.example.com",
    "https://sample-app.example.com:8443",
])
def test_token_transport_allowed(url):
    assert smoke.RequestApi(url, TOKEN, 1.0).base_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://sample-app.example.com",                       # 외부 http
    "http://sample-app:8080",                              # 짧은 이름은 클러스터 내부로 단정할 수 없다
    "http://evil.svc.cluster.local.example.com",           # 접미사 위장
    "http://evil.svc.example.com",
    "http://evil.svc.cluster.local.",                      # 끝 점은 접미사가 아니다
    "http://evilsvc",
    "http://evil-svc.cluster.local",
    "http://svc",
    "http://.svc",
    "http://svc.cluster.local",
    "http://evil.com\\.svc",                               # 호스트 파서마다 해석이 갈리는 이름
    "http://evil.example.com/a.svc",                       # 경로의 .svc 는 호스트가 아니다
    "http://127.0.0.2",
    "http://localhost:8080",
])
def test_token_transport_refused_over_http(url):
    with pytest.raises(SmokeConfigError) as err:
        smoke.RequestApi(url, TOKEN, 1.0)
    assert TOKEN not in str(err.value)


@pytest.mark.parametrize("url", ["http://x.svc@evil.example.com", "http://evil.example.com?host=x.svc",
                                 "http://evil.example.com#x.svc"])
def test_url_tricks_with_cluster_suffix_are_refused(url):
    with pytest.raises(SmokeConfigError):
        smoke.RequestApi(url, TOKEN, 1.0)


def test_cleartext_external_http_is_still_allowed_without_token():
    assert smoke.RequestApi("http://sample-app.example.com", None, 1.0).base_url == "http://sample-app.example.com"


@pytest.mark.parametrize("url", ["ftp://x", "nothing", "http://user:pw@127.0.0.1", "http://127.0.0.1?a=1"])
def test_bad_url_is_config_error_without_echoing_it(url):
    with pytest.raises(SmokeConfigError) as err:
        run_smoke("pre_cutover", job_id=JOB, source_url=url, target_url="http://127.0.0.1:9")
    assert "pw" not in str(err.value)


@pytest.mark.parametrize("value", ["", "short", "x" * 32 + "\nX-Evil: 1", "한글" * 20])
def test_malformed_token_env_is_refused_without_value(value):
    with pytest.raises(SmokeConfigError) as err:
        smoke.token_from_env("T", {"T": value})
    assert value == "" or value not in str(err.value)


def test_token_env_absent_and_valid():
    assert smoke.token_from_env("T", {}) is None
    assert smoke.token_from_env("T", {"T": TOKEN}) == TOKEN


# ---- 설정 오류와 CLI -----------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [{"job_id": ""}, {"job_id": "x" * 200}, {"http_timeout": 0}])
def test_invalid_arguments(kwargs):
    base: dict[str, Any] = {"job_id": JOB, "source_url": "http://127.0.0.1:9", "target_url": "http://127.0.0.1:9"}
    with pytest.raises(SmokeConfigError):
        run_smoke("pre_cutover", **{**base, **kwargs})


def test_only_pre_cutover_and_final_verify_are_supported():
    with pytest.raises(SmokeConfigError):
        run_smoke("pre_start", job_id=JOB, source_url="http://127.0.0.1:9", target_url="http://127.0.0.1:9")


def test_cli_prints_check_and_exit_code(apps, capsys, monkeypatch):
    source, target = apps
    monkeypatch.delenv(smoke.DEFAULT_TOKEN_ENV, raising=False)
    args = ["run", "--stage", "pre_cutover", "--job-id", JOB, "--source-url", source.url, "--target-url", target.url,
            "--replication-timeout", "0.5", "--poll-interval", "0.01"]
    assert smoke.main(args) == 0
    check = json.loads(capsys.readouterr().out)
    assert check["status"] == "pass"
    assert check["detail"]["db_connected"] is True and check["detail"]["order_query_ok"] is True

    args2 = ["run", "--stage", "final_verify", "--job-id", JOB, "--source-url", source.url, "--target-url", target.url,
             "--request-id", check["detail"]["request_id"]]
    assert smoke.main(args2) == 0
    assert json.loads(capsys.readouterr().out)["detail"]["steps"]["create"]["result"] == SKIPPED


def test_cli_exit_codes_for_failures(apps, capsys, monkeypatch):
    source, target = apps
    monkeypatch.delenv(smoke.DEFAULT_TOKEN_ENV, raising=False)
    args = ["run", "--stage", "pre_cutover", "--job-id", JOB, "--source-url", source.url, "--target-url", target.url,
            "--replication-timeout", "0"]
    source.override = only("POST", 409, api_error("REQUEST_CONFLICT"))
    assert smoke.main(args) == 2
    source.override = None
    target.override = only("GET", 401, api_error("UNAUTHORIZED"))
    assert smoke.main(args) == 3
    capsys.readouterr()


def test_cli_reads_token_from_env_only(apps, capsys, monkeypatch):
    source, target = apps
    source.token = target.token = TOKEN
    monkeypatch.setenv(smoke.DEFAULT_TOKEN_ENV, TOKEN)
    args = ["run", "--stage", "pre_cutover", "--job-id", JOB, "--source-url", source.url, "--target-url", target.url,
            "--replication-timeout", "0.5", "--poll-interval", "0.01"]
    assert smoke.main(args) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_cli_config_error_exit_code(capsys, monkeypatch):
    monkeypatch.setenv(smoke.DEFAULT_TOKEN_ENV, "short")
    code = smoke.main(["run", "--stage", "pre_cutover", "--job-id", JOB, "--source-url", "http://127.0.0.1:9",
                       "--target-url", "http://127.0.0.1:9"])
    assert code == 1
    assert "short" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --target-only: 대상 앱의 DB 조회만 본다
# ---------------------------------------------------------------------------


def test_target_only_passes_on_app_404_without_writing(apps):
    source, target = apps
    result = run_smoke("pre_cutover", job_id=JOB, target_url=target.url, target_only=True)
    check = result.to_check()
    assert check["status"] == "pass"
    assert check["detail"]["db_connected"] is True and check["detail"]["order_query_ok"] is True
    assert check["detail"]["mode"] == "target_only"
    assert result.exit_code == smoke.EXIT_PASS
    assert source.calls == [] and target.posts() == [] and target.rows == {}


def test_target_only_db_unavailable_is_confirmed_failure(apps):
    _, target = apps
    target.override = lambda *_: (503, api_error("DB_UNAVAILABLE"), {})
    check = run_smoke("pre_cutover", job_id=JOB, target_url=target.url, target_only=True).to_check()
    assert check["status"] == "pass"  # 관측 성공, 사실은 detail 로 전한다 (OPA deny)
    assert check["detail"]["db_connected"] is False and check["detail"]["order_query_ok"] is False


def test_target_only_ingress_404_is_not_db_evidence(apps):
    _, target = apps
    target.override = lambda *_: (404, {"message": "not found"}, {})
    check = run_smoke("pre_cutover", job_id=JOB, target_url=target.url, target_only=True).to_check()
    assert check["status"] == "error" and check["detail"]["db_connected"] is False


def test_target_only_unreachable_is_error():
    check = run_smoke("pre_cutover", job_id=JOB, target_url="http://127.0.0.1:9", target_only=True, http_timeout=1).to_check()
    assert check["status"] == "error"


def test_target_only_is_refused_on_final_verify():
    with pytest.raises(SmokeConfigError):
        run_smoke("final_verify", job_id=JOB, target_url="http://t", target_only=True)


def test_source_url_still_required_without_target_only():
    with pytest.raises(SmokeConfigError):
        run_smoke("pre_cutover", job_id=JOB, target_url="http://t")


def test_cli_target_only(apps, capsys, monkeypatch):
    monkeypatch.delenv("SMOKE_API_TOKEN", raising=False)
    _, target = apps
    assert smoke.main(["run", "--stage", "pre_cutover", "--job-id", JOB, "--target-only", "--target-url", target.url]) == 0
    assert json.loads(capsys.readouterr().out)["detail"]["mode"] == "target_only"
