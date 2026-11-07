import dataclasses
import http.client
import json
import threading

import fake_ingress
import pytest

from crosscloud import control_adapter, ingress_control

_CONFIG = control_adapter.AdapterConfig(
    source="aws",
    target="gcp",
    port=0,
    source_app_url="http://aws-app.example",
    target_app_url="http://gcp-app.example",
    target_database="shop_b",
)


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Readiness:
    """대상 앱 신원 대조(app_identity_problems) 가짜. ready 면 문제 없음."""

    def __init__(self, ready=True):
        self.ready = ready
        self.checked = []

    def __call__(self, base_url, *, database, environment, timeout):
        self.checked.append((base_url, database))
        ready = self.ready() if callable(self.ready) else self.ready
        return () if ready else ("database=shop (기대 shop_b)",)


@pytest.fixture
def ingress():
    fake = fake_ingress.FakeIngress()
    fake.state.update(mode="running", target="aws", epoch="1")
    with fake.serve() as url:
        yield fake, url


class _Identity:
    def __init__(self, problems=()):
        self.problems = problems
        self.inspected = []

    def __call__(self, base_url, *, database, environment, timeout):
        self.inspected.append((base_url, database, environment))
        return self.problems


def _adapter(url, readiness=None, fake_time=None, identity=None, config=None):
    fake_time = fake_time or _FakeTime()
    return control_adapter.ControlAdapter(
        config or _CONFIG,
        ingress_control.IngressControl(url, token=None),
        inspect_app=identity or readiness or _Identity(),
        clock=fake_time.clock,
        sleep=fake_time.sleep,
    )


def test_status_maps_ingress_mode_to_app_state(ingress):
    _, url = ingress

    code, body = _adapter(url).status()

    assert code == 200
    assert body == {
        "state": "RUNNING",
        "target": "aws",
        "known_targets": ["aws", "gcp"],
        "in_flight": 0,
    }


def test_status_rejects_ingress_target_outside_the_job(ingress):
    fake, url = ingress
    fake.state["target"] = "onprem"

    code, body = _adapter(url).status()

    assert code == 502
    assert "onprem" in body["error"]


def test_pause_maps_to_ingress_pause(ingress):
    fake, url = ingress

    code, body = _adapter(url).pause()

    assert code == 200
    assert body["state"] == "PAUSED"
    assert fake.state["mode"] == "paused"


def test_drain_settles_when_paused_and_idle(ingress):
    fake, url = ingress
    fake.state["mode"] = "paused"

    code, body = _adapter(url).drain({"timeout": 120})

    assert code == 200
    assert body["settled"] is True
    assert body["in_flight"] == 0


def test_drain_waits_for_in_flight_delivery(ingress):
    fake, url = ingress
    fake.state.update(mode="paused", in_flight=1)
    fake_time = _FakeTime()
    original_sleep = fake_time.sleep

    def finish_delivery(seconds):
        original_sleep(seconds)
        if fake_time.now >= 2:
            fake.state["in_flight"] = 0

    fake_time.sleep = finish_delivery

    code, body = _adapter(url, fake_time=fake_time).drain({"timeout": 120})

    assert code == 200
    assert body["settled"] is True
    assert fake_time.now >= 2


def test_drain_not_settled_at_timeout_returns_409(ingress):
    fake, url = ingress
    fake.state.update(mode="paused", in_flight=1)

    code, body = _adapter(url).drain({"timeout": 5})

    assert code == 409
    assert body["settled"] is False


def test_drain_not_settled_while_running(ingress):
    _, url = ingress

    code, body = _adapter(url).drain({"timeout": 3})

    assert code == 409
    assert body["settled"] is False


@pytest.mark.parametrize("payload", [{}, {"timeout": "x"}, {"timeout": -1}])
def test_drain_rejects_bad_timeout(ingress, payload):
    _, url = ingress

    code, _ = _adapter(url).drain(payload)

    assert code == 400


def test_target_checks_app_db_identity_then_switches_with_epoch(ingress):
    fake, url = ingress
    fake.state["mode"] = "paused"
    identity = _Identity()

    code, body = _adapter(url, identity=identity).target({"target": "gcp"})

    assert code == 200
    assert body["state"] == "PAUSED"
    assert body["target"] == "gcp"
    assert identity.inspected == [("http://gcp-app.example", "shop_b", None)]
    assert (
        "POST",
        "/target",
        {"target": "gcp", "expected_epoch": "1"},
    ) in fake.calls


def test_target_refuses_when_target_app_is_not_on_target_db(ingress):
    fake, url = ingress
    fake.state["mode"] = "paused"
    identity = _Identity(("database=shop (기대 shop_b)",))

    code, body = _adapter(url, identity=identity).target({"target": "gcp"})

    assert code == 503
    assert fake.state["target"] == "aws"
    assert "database=shop" in body["error"]


def test_labels_map_to_configured_ingress_target_names(ingress):
    fake, url = ingress
    fake.targets.update({"aws_app", "gcp_app"})
    fake.state.update(mode="paused", target="aws_app")
    config = dataclasses.replace(
        _CONFIG, source_ingress_name="aws_app", target_ingress_name="gcp_app"
    )
    adapter = _adapter(url, config=config)

    status_code, status = adapter.status()
    target_code, switched = adapter.target({"target": "gcp"})

    assert (status_code, status["target"]) == (200, "aws")
    assert (target_code, switched["target"]) == (200, "gcp")
    assert fake.state["target"] == "gcp_app"


def test_target_refuses_while_running(ingress):
    fake, url = ingress

    code, _ = _adapter(url).target({"target": "gcp"})

    assert code == 409
    assert fake.state["target"] == "aws"


@pytest.mark.parametrize("payload", [{"target": "aws"}, {"target": "x"}, {}])
def test_target_accepts_only_the_job_target_label(ingress, payload):
    fake, url = ingress
    fake.state["mode"] = "paused"

    code, _ = _adapter(url).target(payload)

    assert code == 409
    assert fake.state["target"] == "aws"


def test_target_is_idempotent_once_switched(ingress):
    fake, url = ingress
    fake.state.update(mode="paused", target="gcp", epoch="2")

    code, body = _adapter(url).target({"target": "gcp"})

    assert code == 200
    assert body["target"] == "gcp"
    assert fake.state["epoch"] == "2"


def test_resume_reopens_delivery_only_on_job_target(ingress):
    fake, url = ingress
    fake.state.update(mode="paused", target="gcp", epoch="2")

    code, body = _adapter(url).resume()

    assert code == 200
    assert body["state"] == "RUNNING"
    assert ("POST", "/resume", {"expected_epoch": "2"}) in fake.calls


def test_resume_refuses_on_source(ingress):
    fake, url = ingress
    fake.state["mode"] = "paused"

    code, body = _adapter(url).resume()

    assert code == 409
    assert fake.state["mode"] == "paused"
    assert "aws" in body["error"]


def test_resume_is_idempotent_when_running(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp", epoch="2")

    code, _ = _adapter(url).resume()

    assert code == 200
    assert not any(path == "/resume" for _, path, _ in fake.calls)


def test_healthz_needs_consecutive_target_readiness(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp")
    results = iter([True, False, True, True, True])
    readiness = _Readiness(ready=lambda: next(results))

    code, body = _adapter(url, readiness).healthz()

    assert code == 200
    assert body == {"healthy": True, "target": "gcp"}
    assert set(readiness.checked) == {("http://gcp-app.example", "shop_b")}


def test_healthz_unhealthy_within_deadline_is_503(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp")

    code, body = _adapter(url, _Readiness(ready=False)).healthz()

    assert code == 503
    assert body == {"healthy": False, "target": "gcp"}


def test_ingress_failure_is_reported_as_502():
    adapter = _adapter("http://127.0.0.1:9")

    for code, body in (adapter.status(), adapter.pause(), adapter.healthz()):
        assert code == 502
        assert "error" in body


def test_start_on_source_moves_fresh_ingress_to_source_and_resumes():
    fake = fake_ingress.FakeIngress()
    with fake.serve() as url:
        code, body = _adapter(url).start_on_source()

    assert code == 200
    assert body["state"] == "RUNNING"
    assert fake.state["target"] == "aws"


def test_start_on_source_refuses_after_switch_to_target():
    fake = fake_ingress.FakeIngress()
    fake.state.update(mode="paused", target="gcp", epoch="3")
    with fake.serve() as url:
        code, _ = _adapter(url).start_on_source()

    assert code == 409
    assert fake.state["target"] == "gcp"


def test_load_config_from_rendered_files(tmp_path):
    (tmp_path / "config.cloud.yaml").write_text(
        "targets: {gcp: {dbname: shop_b}}\n"
        "job: {job_id: j, source: aws, target: gcp}\n"
        "application: {control_url: 'http://127.0.0.1:18080'}\n"
    )
    targets = {
        "aws": {"url": "http://aws-app.example"},
        "gcp": {"url": "http://gcp-app.example"},
        "onprem": {"url": "http://aws-app.example"},
    }
    (tmp_path / "ingress.env").write_text(
        "# comment\nREDIS_URL=redis://redis:6379\n"
        f"INGRESS_TARGETS={json.dumps(targets)}\n"
    )

    config = control_adapter.load_adapter_config(
        tmp_path / "config.cloud.yaml", tmp_path / "ingress.env"
    )

    assert config == control_adapter.AdapterConfig(
        source="aws",
        target="gcp",
        port=18080,
        source_app_url="http://aws-app.example",
        target_app_url="http://gcp-app.example",
        target_database="shop_b",
    )


def test_load_config_refuses_missing_label_target(tmp_path):
    (tmp_path / "config.cloud.yaml").write_text(
        "job: {job_id: j, source: aws, target: gcp}\n"
        "application: {control_url: 'http://127.0.0.1:18080'}\n"
    )
    (tmp_path / "ingress.env").write_text(
        'INGRESS_TARGETS={"aws": {"url": "http://a"}}\n'
    )

    with pytest.raises(ValueError, match="gcp"):
        control_adapter.load_adapter_config(
            tmp_path / "config.cloud.yaml", tmp_path / "ingress.env"
        )


def _serve(adapter):
    server = control_adapter.make_server(adapter, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_server_listens_on_loopback_only(ingress):
    _, url = ingress
    server, thread = _serve(_adapter(url))
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_migration_worker_client_drives_full_cutover_sequence(
    ingress, migration_module
):
    worker_client = migration_module("migration.worker_client")
    fake, url = ingress
    server, thread = _serve(_adapter(url))
    client = worker_client.HttpWorkerClient(
        f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        # migration/cloud/runner.py 가 단계마다 확인하는 값 그대로.
        precheck = client.status()
        assert (precheck["state"], precheck["target"]) == ("RUNNING", "aws")
        assert set(precheck["known_targets"]) == {"aws", "gcp"}
        client.pause()
        assert client.drain(timeout=120)["settled"] is True
        paused = client.status()
        assert (paused["state"], paused["in_flight"]) == ("PAUSED", 0)
        assert paused["target"] == "aws"
        client.switch_target("gcp")
        switched = client.status()
        assert (switched["state"], switched["target"]) == ("PAUSED", "gcp")
        assert switched["in_flight"] == 0
        client.resume()
        running = client.status()
        assert (running["state"], running["target"]) == ("RUNNING", "gcp")
        assert client.healthz() == {"healthy": True, "target": "gcp"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_migration_worker_client_sees_drain_not_settled(
    ingress, migration_module
):
    worker_client = migration_module("migration.worker_client")
    fake, url = ingress
    fake.state.update(mode="paused", in_flight=1)
    server, thread = _serve(_adapter(url))
    client = worker_client.HttpWorkerClient(
        f"http://127.0.0.1:{server.server_address[1]}"
    )
    try:
        assert client.drain(timeout=1)["settled"] is False
        with pytest.raises(worker_client.WorkerClientError):
            client.resume()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "cloud_yaml",
    [
        "job: {job_id: j}\n",
        "job: {job_id: j, source: aws, target: azure}\n"
        "application: {control_url: 'http://127.0.0.1:18080'}\n",
        "job: {job_id: j, source: aws, target: gcp}\n"
        "application: {control_url: 'http://127.0.0.1'}\n",
    ],
)
def test_load_config_refuses_malformed_cloud_config(tmp_path, cloud_yaml):
    (tmp_path / "config.cloud.yaml").write_text(cloud_yaml)
    (tmp_path / "ingress.env").write_text(
        'INGRESS_TARGETS={"aws": {"url": "http://a"}, "gcp": {"url": "http://g"}}\n'
    )

    with pytest.raises(ValueError):
        control_adapter.load_adapter_config(
            tmp_path / "config.cloud.yaml", tmp_path / "ingress.env"
        )


def test_healthz_unhealthy_when_ingress_paused_itself_after_resume(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp")
    readiness = _Readiness()

    def stop_delivery():
        fake.state.update(mode="paused", last_error="CONTENT_CONFLICT")
        return True

    readiness.ready = stop_delivery

    code, body = _adapter(url, readiness).healthz()

    assert code == 503
    assert body == {"healthy": False, "target": "gcp"}


def test_healthz_unhealthy_while_delivery_retrying(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp", last_error="DELIVERY_RETRY")

    code, body = _adapter(url).healthz()

    assert code == 503
    assert body["healthy"] is False


def test_healthz_rechecks_ingress_just_before_answering(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp")
    calls = []

    def ready_then_ingress_stops():
        calls.append(1)
        if len(calls) == 3:
            fake.state.update(mode="paused", last_error="LEASE_LOST")
        return True

    code, body = _adapter(url, _Readiness(ready_then_ingress_stops)).healthz()

    assert len(calls) == 3
    assert code == 503
    assert body["healthy"] is False


def test_healthz_answers_within_migration_http_limit(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp")
    fake_time = _FakeTime()

    def slow_not_ready():
        fake_time.now += 6  # /readyz·/api/info 호출 상한만큼 걸린다고 본다
        return False

    code, _ = _adapter(
        url, _Readiness(slow_not_ready), fake_time=fake_time
    ).healthz()

    assert code == 503
    assert fake_time.now <= 25 - ingress_control.CALL_TIMEOUT_SECONDS


def test_healthz_unhealthy_when_queue_points_at_source(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="aws")

    code, body = _adapter(url).healthz()

    assert code == 503
    assert body == {"healthy": False, "target": "aws"}


def test_target_checks_pause_before_idempotent_answer(ingress):
    fake, url = ingress
    fake.state.update(mode="running", target="gcp", in_flight=1)

    code, _ = _adapter(url).target({"target": "gcp"})

    assert code == 409


def test_load_config_refuses_same_database_name(tmp_path):
    (tmp_path / "config.cloud.yaml").write_text(
        "targets: {aws: {dbname: shop}, gcp: {dbname: shop}}\n"
        "job: {job_id: j, source: aws, target: gcp}\n"
        "application: {control_url: 'http://127.0.0.1:18080'}\n"
    )
    (tmp_path / "ingress.env").write_text(
        'INGRESS_TARGETS={"aws":{"url":"http://a"},"gcp":{"url":"http://g"}}\n'
    )

    with pytest.raises(ValueError, match="dbname"):
        control_adapter.load_adapter_config(
            tmp_path / "config.cloud.yaml", tmp_path / "ingress.env"
        )


def test_server_rejects_malformed_content_length(ingress):
    _, url = ingress
    server, thread = _serve(_adapter(url))
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=5
        )
        connection.putrequest("POST", "/drain")
        connection.putheader("Content-Length", "abc")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
