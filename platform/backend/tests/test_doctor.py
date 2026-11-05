from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_argo import harness as harness
from test_argo import sdk as sdk

from controlplane.execution.onprem import doctor
from controlplane.providers.onprem.preflight import PermissionCheck, PreflightCode, PreflightReport


def ready_report():
    return PreflightReport(ready_for_sdk=True, permissions=[], code=PreflightCode.BINDINGS_PRESENT)


def diagnose(harness, **kwargs):
    return doctor.diagnose(
        harness.runtime.settings,
        harness.request,
        access_check=kwargs.pop("access_check", lambda *_: ready_report()),
        endpoint_check=kwargs.pop("endpoint_check", Mock()),
        **kwargs,
    )


def test_db_without_application_dependencies_and_no_release_created(harness):
    assert harness.request.resources.database == "none"
    assert harness.request.resources.redis is False
    result = diagnose(harness)
    assert result["checks_passed"]
    assert result["application_verified"] is False
    assert harness.store.list() == []
    assert result["unverified"]


def test_missing_service_account_permission_is_reported_without_writes(harness):
    result = diagnose(
        harness,
        access_check=lambda *_: PreflightReport(
            ready_for_sdk=False,
            code=PreflightCode.RBAC_DENIED,
            permissions=[
                PermissionCheck(
                    namespace="oneport-apps",
                    group="",
                    resource="serviceaccounts",
                    verb="get",
                    allowed=False,
                )
            ],
        ),
    )
    assert not result["checks_passed"]
    failed = [c for c in result["checks"] if not c["passed"]]
    assert failed[0]["code"] == "rbac_denied"
    assert failed[0]["denied"][0]["resource"] == "serviceaccounts"
    assert harness.store.list() == []


def test_api_access_is_not_mistaken_for_cluster_dns_access(harness):
    def endpoint(host):
        if host.startswith("kubernetes.default.svc."):
            raise OSError("private routing unavailable")

    result = diagnose(harness, endpoint_check=endpoint)
    assert not result["checks_passed"]
    assert {c["name"] for c in result["checks"] if not c["passed"]} == {
        "cluster_service_dns_and_tcp"
    }


def test_job_mode_checks_public_route_without_claiming_private_http_verified(harness):
    harness.runtime.settings.preview_probe_mode = "job"
    harness.runtime.settings.preview_probe_image = harness.request.source.image
    endpoint = Mock()
    result = diagnose(harness, endpoint_check=endpoint)
    assert result["checks_passed"]
    assert not result["application_verified"]
    endpoint.assert_called_once_with(harness.runtime.settings.environment.hostname)
    assert any(c["name"] == "preview_job_configured" for c in result["checks"])


def test_job_mode_preflight_requests_job_permissions(harness, monkeypatch):
    harness.runtime.settings.preview_probe_mode = "job"
    clients = Mock()
    preflight = Mock(return_value=ready_report())
    monkeypatch.setattr(doctor, "connect", Mock(return_value=clients))
    monkeypatch.setattr(doctor, "preflight", preflight)
    doctor.check_access(harness.runtime.settings, harness.request)
    assert preflight.call_args.kwargs["preview_job"] is True
    clients.close.assert_called_once()


def test_invalid_policy_does_not_contact_db_or_cluster(harness):
    harness.runtime.settings.allowed_images = []
    dependencies = Mock()
    result = diagnose(harness, database_check=dependencies, access_check=dependencies)
    assert not result["checks_passed"]
    dependencies.assert_not_called()


@pytest.mark.parametrize("dependency", ["database_check", "access_check", "endpoint_check"])
def test_raw_errors_never_leak_credentials(harness, dependency):
    result = diagnose(harness, **{dependency: Mock(side_effect=RuntimeError("DO-NOT-LEAK"))})
    assert not result["checks_passed"]
    assert "DO-NOT-LEAK" not in str(result)


def test_missing_schema_is_reported_and_not_created(store):
    with store.connect() as conn:
        conn.execute("DROP TABLE release_events, releases CASCADE")
    settings = SimpleNamespace(database_url=SimpleNamespace(get_secret_value=lambda: store.dsn))
    with pytest.raises(Exception):
        doctor.check_database(settings)
    with store.connect() as conn:
        assert conn.execute("SELECT to_regclass('releases') AS name").fetchone()["name"] is None


def test_invalid_config_has_safe_cli_output(tmp_path, capsys):
    config = tmp_path / "runtime.json"
    config.write_text('{"database_url":"DO-NOT-LEAK"}')
    assert doctor.main(["--config", str(config), "--request", str(tmp_path / "request.json")]) == 1
    assert "DO-NOT-LEAK" not in capsys.readouterr().out
