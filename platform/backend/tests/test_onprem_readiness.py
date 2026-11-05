from types import SimpleNamespace
from unittest.mock import Mock

from controlplane.execution.onprem import readiness


def clients(*, denied=()):
    def review(*, body, **kwargs):
        attrs = body["spec"]["resourceAttributes"]
        assert attrs["namespace"] == "oneport-apps"
        assert attrs["resource"] not in {"nodes", "namespaces", "roles", "persistentvolumes"}
        allowed = (attrs["resource"], attrs["verb"]) not in denied
        return SimpleNamespace(status=SimpleNamespace(allowed=allowed))

    return SimpleNamespace(
        authorization=SimpleNamespace(create_self_subject_access_review=Mock(side_effect=review)),
        core=SimpleNamespace(
            list_namespaced_pod=Mock(return_value=SimpleNamespace(items=[])),
            read_namespaced_service_account=Mock(),
        ),
        custom=SimpleNamespace(list_namespaced_custom_object=Mock(return_value={"items": []})),
    )


def test_api_success_does_not_hide_network_and_data_blockers(monkeypatch):
    monkeypatch.setattr(readiness, "cluster_dns", lambda: "unresolved")
    c = clients(denied={("persistentvolumeclaims", "create"), ("jobs", "delete")})
    result = readiness.inspect(c)
    assert result["api_connected"] and result["resource_counts"] == {"pods": 0, "rollouts": 0}
    assert result["application_verified"] is False
    assert result["application_execution_blockers"] == [
        "worker_cluster_dns_unresolved",
        "worker_platform_database_not_verified",
    ]
    assert {(r["resource"], r["verb"]) for r in result["data_permission_blockers"]} == {
        ("persistentvolumeclaims", "create"),
        ("jobs", "delete"),
    }


def test_resolved_network_still_does_not_claim_application_success(monkeypatch):
    monkeypatch.setattr(readiness, "cluster_dns", lambda: "resolved")
    monkeypatch.setattr(readiness, "database_check", lambda path: "reachable_from_this_process")
    result = readiness.inspect(clients())
    assert result["application_execution_blockers"] == []
    assert not result["application_verified"]
    assert result["unverified"]


def test_application_permission_failure_is_reported(monkeypatch):
    monkeypatch.setattr(readiness, "cluster_dns", lambda: "resolved")
    result = readiness.inspect(clients(denied={("rollouts", "update")}))
    assert "application_rbac_missing" in result["application_execution_blockers"]


def test_database_failure_never_returns_secret(tmp_path, monkeypatch, capsys):
    config = tmp_path / "database.json"
    config.write_text('{"database_url":"postgresql://user:PRIVATE_PASSWORD@localhost/db"}')
    monkeypatch.setattr(
        readiness.psycopg, "connect", Mock(side_effect=ValueError("PRIVATE_PASSWORD"))
    )
    assert readiness.database_check(config) == "unreachable_or_invalid_config"
    assert "PRIVATE_PASSWORD" not in capsys.readouterr().out


def test_database_check_is_read_only(tmp_path, monkeypatch):
    config = tmp_path / "database.json"
    config.write_text('{"database_url":"test-dsn"}')
    db = Mock()
    db.execute.return_value.fetchone.return_value = (1,)
    context = Mock(__enter__=Mock(return_value=db), __exit__=Mock(return_value=False))
    monkeypatch.setattr(readiness.psycopg, "connect", Mock(return_value=context))
    assert readiness.database_check(config) == "reachable_from_this_process"
    db.execute.assert_called_once_with("SELECT 1")
