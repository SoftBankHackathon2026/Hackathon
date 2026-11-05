"""Shared DB contract: reuse operator-prepared credentials, never provision an RDS instance.
Dedicated DB/cache are opt-in at registration (see test_dedicated_resources.py)."""

import pytest
from pydantic import ValidationError

from controlplane.domain.models import ResourceRequirements
from controlplane.schemas.environments import ApplicationRegistration
from oneport_aws.adapter import AwsAdapter
from oneport_aws.execution.oneport_ecs_apps import AppRegistry

from .conftest import SECRET_REF
from .test_worker import harness  # noqa: F401


def test_shared_registration_reuses_secret_without_rds(harness):  # noqa: F811
    h = harness
    h.settings.secret_refs = {"db": SECRET_REF}
    adapter = AwsAdapter(h.settings)
    adapter.initialize()
    result = adapter.register_application(port=8080, health_path="/health", database="postgresql")
    assert result["application_registration"]["database_mode"] == "shared"
    assert result["application_registration"]["db_state"] == "ready"
    registry = AppRegistry(h.store.dsn)
    assert registry.refs("shop", "aws-oneport") == {"db": SECRET_REF}
    h.worker.tick()
    assert not any("rds." in call for call in h.aws.calls)
    payload = h.make_request(resources={"database": "postgresql"}).model_dump(mode="json")
    assert adapter.parse_request(payload).resources.database_mode == "shared"
    # A repeat registration doesn't rotate or duplicate the credentials.
    adapter.register_application(port=8080, health_path="/health", database="postgresql")
    assert registry.refs("shop", "aws-oneport") == {"db": SECRET_REF}


def test_missing_shared_binding_does_not_request_dedicated(harness):  # noqa: F811
    adapter = AwsAdapter(harness.settings)
    with pytest.raises(ValueError, match="Secret"):
        adapter.register_application(port=8080, health_path="/health", database="postgresql")
    app = adapter.apps.get("shop", "aws-oneport")
    assert app.database == "none" and app.db_state == "none"


def test_public_contract_defaults_to_shared_and_accepts_dedicated():
    assert ResourceRequirements(database="postgresql").database_mode == "shared"
    assert ApplicationRegistration(database="postgresql").redis is False
    ResourceRequirements(database="postgresql", database_mode="dedicated")
    ApplicationRegistration(database="postgresql", database_mode="dedicated", redis=True)
    with pytest.raises(ValidationError):
        ApplicationRegistration(database="postgresql", database_mode="managed")


@pytest.mark.parametrize("key", ["DATABASE_URL", "REDIS_URL", "PORT", "LAUNCHPAD_RELEASE_ID"])
def test_connection_values_cannot_enter_plain_environment(make_request, key):
    with pytest.raises(ValidationError):
        make_request(env={key: "must-use-secret-binding"})


def test_switching_existing_dedicated_database_is_not_implicit(harness):  # noqa: F811
    registry = AppRegistry(harness.store.dsn)
    registry.register(
        harness.settings.environment(), port=8080, health_path="/health", database="postgresql"
    )
    harness.settings.secret_refs = {"db": SECRET_REF}
    adapter = AwsAdapter(harness.settings)
    with pytest.raises(ValueError, match="이전"):
        adapter.register_application(port=8080, health_path="/health", database="postgresql")
    assert registry.get("shop", "aws-oneport").db_state == "requested"
    assert registry.refs("shop", "aws-oneport") == {}


def test_shared_web_registration_and_release_intake(harness):  # noqa: F811
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    h = harness
    h.settings.secret_refs = {"db": SECRET_REF}
    registry = ProviderRegistry()
    registry.register(AwsAdapter(h.settings))
    token = "shared-demo-test-operator-token-0123456789"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(registry=registry, store=h.store, operator_token=token)) as client:
        route = "/api/v1/environments/aws-oneport/application"
        body = {
            "port": 8080,
            "health_path": "/health",
            "database": "postgresql",
            "database_mode": "shared",
        }
        assert client.post(route, json=body).status_code == 401
        registered = client.post(route, json=body, headers=headers)
        assert registered.status_code == 200
        request = h.make_request(resources={"database": "postgresql"}).model_dump(mode="json")
        queued = client.post(
            "/api/v1/releases",
            json={"request": request, "idempotency_key": "shared-web-test-key-001"},
            headers=headers,
        )
        assert queued.status_code == 202
        assert client.post(route, json=body, headers=headers).status_code == 409
        assert SECRET_REF not in registered.text
        assert not h.aws.calls


def test_shared_registration_never_starts_rds_in_the_worker(harness):  # noqa: F811
    h = harness
    h.settings.secret_refs = {"db": SECRET_REF}
    AwsAdapter(h.settings).register_application(
        port=8080, health_path="/health", database="postgresql", database_mode="shared"
    )
    for _ in range(3):
        h.worker.tick()
    assert not any(call.startswith(("rds.", "elasticache.")) for call in h.aws.calls)


def test_required_database_is_enforced_at_http_registration_and_intake(harness):  # noqa: F811
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    h = harness
    h.settings.required_database = "postgresql"
    h.settings.secret_refs = {"db": SECRET_REF}
    adapter = AwsAdapter(h.settings)
    registry = ProviderRegistry()
    registry.register(adapter)
    token = "required-db-test-operator-token-0123456789"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(registry=registry, store=h.store, operator_token=token)) as client:
        route = "/api/v1/environments/aws-oneport/application"
        rejected = client.post(route, json={"database": "none"}, headers=headers)
        assert rejected.status_code == 409
        assert adapter.apps.get("shop", "aws-oneport").database == "none"
        # Old DB-free registration cannot bypass a newly required app contract.
        payload = h.make_request().model_dump(mode="json")
        rejected = client.post(
            "/api/v1/releases",
            json={"request": payload, "idempotency_key": "db-required-missing-001"},
            headers=headers,
        )
        assert rejected.status_code == 422
        assert h.store.list() == []
        assert not h.aws.calls
        registered = client.post(route, json={"database": "postgresql"}, headers=headers)
        assert registered.status_code == 200
        assert registered.json()["application_registration"]["required_database"] == "postgresql"


def test_operator_reads_recent_app_logs_over_http(harness):  # noqa: F811
    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    h = harness
    h.aws.log_events = [
        {"timestamp": 1_700_000_000_000, "logStreamName": "shop/app/t1", "message": "ready\n"},
        {"timestamp": 1_700_000_000_001, "logStreamName": "blog/app/t2", "message": "other app"},
    ]
    registry = ProviderRegistry()
    registry.register(AwsAdapter(h.settings, clients=h.aws))
    token = "app-logs-test-operator-token-0123456789"
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(registry=registry, store=h.store, operator_token=token)) as client:
        route = "/api/v1/environments/aws-oneport/logs"
        assert client.get(route).status_code == 401
        found = client.get(route, params={"minutes": 15}, headers=headers)
        assert found.status_code == 200
        body = found.json()
        assert [line["message"] for line in body["lines"]] == ["ready"]
        assert body["lines"][0]["stream"] == "shop/app/t1"
        assert body["minutes"] == 15 and body["truncated"] is False
        assert client.get(route, params={"q": 'a"b'}, headers=headers).status_code == 422
        assert client.get(route, params={"minutes": 0}, headers=headers).status_code == 422
        missing = client.get("/api/v1/environments/aws-missing/logs", headers=headers)
        assert missing.status_code == 409
    assert all(call.startswith("logs.") for call in h.aws.calls)


def test_operator_reads_traffic_and_traces_over_http(harness):  # noqa: F811
    import json

    from fastapi.testclient import TestClient

    from controlplane.main import create_app
    from controlplane.providers.registry import ProviderRegistry

    h = harness
    trace_id = "1-66000010-" + "b" * 24
    h.aws.trace_summaries = [{"Id": trace_id, "Duration": 0.2, "Http": {"HttpStatus": 200}}]
    document = {"name": "shop", "id": "s1", "start_time": 1.0, "end_time": 1.2}
    h.aws.traces = [{"Id": trace_id, "Segments": [{"Id": "s1", "Document": json.dumps(document)}]}]
    token = "telemetry-test-operator-token-0123456789"
    headers = {"Authorization": "Bearer " + token}

    def client_for(adapter):
        registry = ProviderRegistry()
        registry.register(adapter)
        return TestClient(create_app(registry=registry, store=h.store, operator_token=token))

    with client_for(AwsAdapter(h.settings, clients=h.aws)) as client:
        base = "/api/v1/environments/aws-oneport"
        assert client.get(base + "/traffic").status_code == 401
        traffic = client.get(base + "/traffic", params={"minutes": 30}, headers=headers)
        assert traffic.status_code == 200 and traffic.json()["series"] == []
        listed = client.get(base + "/traces", headers=headers).json()
        assert listed["tracing_enabled"] is False and listed["traces"] == []

    with h.settings.env_file.open("a") as env_file:
        env_file.write("ONEPORT_TRACING=xray\n")
    with client_for(AwsAdapter(h.settings, clients=h.aws)) as client:
        listed = client.get(base + "/traces", params={"kind": "errors"}, headers=headers)
        assert listed.json()["tracing_enabled"] is True
        assert [t["id"] for t in listed.json()["traces"]] == [trace_id]
        assert (
            client.get(base + "/traces", params={"kind": "x"}, headers=headers).status_code == 422
        )
        detail = client.get(f"{base}/traces/{trace_id}", headers=headers)
        assert detail.status_code == 200 and detail.json()["spans"][0]["name"] == "shop"
        missing = client.get(f"{base}/traces/1-66000010-{'d' * 24}", headers=headers)
        assert missing.status_code == 404
        bad = client.get(f"{base}/traces/not-a-trace", headers=headers)
        assert bad.status_code == 422
    assert "cloudwatch.get_metric_data" in h.aws.calls
    assert all(
        call.startswith(("logs.", "xray.", "ecs.", "elbv2."))
        or call == "cloudwatch.get_metric_data"
        for call in h.aws.calls
    )
