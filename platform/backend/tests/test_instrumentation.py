"""Agent selection and preservation of application startup options."""

import pytest

from controlplane.domain.instrumentation import PATH, agent_environment, clean_environment, detect


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"Env": ["PYTHON_VERSION=3.13"]}, "python"),
        ({"Env": ["NODE_VERSION=22"]}, "nodejs"),
        ({"Entrypoint": ["java", "-jar", "app.jar"]}, "java"),
        ({"Cmd": ["dotnet", "App.dll"]}, "dotnet"),
        ({"Cmd": ["/app/server"]}, "sdk"),
    ],
)
def test_runtime_detection_does_not_guess_unknown_binaries(config, expected):
    assert detect(config) == expected


@pytest.mark.parametrize(
    "kind,original",
    [
        ("java", {"JAVA_TOOL_OPTIONS": '-Xmx128m -Dname="hello world"'}),
        ("nodejs", {"NODE_OPTIONS": "--max-old-space-size=256"}),
        ("dotnet", {"DOTNET_STARTUP_HOOKS": "/app/Existing.dll"}),
    ],
)
def test_agent_options_are_reversible_without_losing_user_settings(kind, original):
    injected = {**original, **agent_environment(kind, original)}
    cleaned = clean_environment(injected)
    for key, value in original.items():
        assert cleaned[key] == value
    assert not any(PATH + "/" in value for value in cleaned.values())


def test_existing_profiler_is_not_replaced():
    with pytest.raises(ValueError, match="profiler"):
        agent_environment("dotnet", {"CORECLR_PROFILER": "another-profiler"})
