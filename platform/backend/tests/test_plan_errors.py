from controlplane.services.plan_errors import plan_error


def test_plan_error_gives_specific_action_without_echoing_inputs():
    assert "PostgreSQL" in plan_error(ValueError("This application requires PostgreSQL"))
    assert "등록된 앱 이름" in plan_error(ValueError("Wrong Helm binding"))
    assert "변경할 수 없습니다" in plan_error(
        ValueError("Existing Helm strategy is immutable; use a new application")
    )
    for secret in ["postgresql://admin:secret@db", "/run/secrets/kubeconfig", "secret-token"]:
        assert secret not in plan_error(ValueError(secret))
