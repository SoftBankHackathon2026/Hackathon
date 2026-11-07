import json
import pathlib
import subprocess

import pytest

from crosscloud.provision import terraform

_CANARY = "canary-7f3a-sensitive-should-not-leak"


class FakeRun:
    """subprocess.run 을 대신해 terraform 호출을 기록하고 정해 둔 결과를 낸다.

    결과는 하위 명령(init·plan·show·apply·destroy·state·output)별로 정한다.
    plan 을 받으면 -out 파일을 실제로 만든다 — terraform 이 그렇게 하므로.
    """

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, args, *, env, capture_output, text, check):
        assert capture_output and text and not check
        self.calls.append((list(args), dict(env)))
        subcommand = args[2]
        if (
            subcommand == "plan"
            and self.results.get("plan", _ok()).returncode == 0
        ):
            out = next(a for a in args if a.startswith("-out="))
            pathlib.Path(out.removeprefix("-out=")).write_text(_CANARY)
        return self.results.get(subcommand, _ok())

    def subcommands(self):
        return [args[2] for args, _ in self.calls]


def _ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr):
    return subprocess.CompletedProcess(
        [], 1, stdout=f"stdout {_CANARY}", stderr=stderr
    )


@pytest.fixture
def paths(tmp_path):
    module = tmp_path / "module"
    module.mkdir()
    var_file = tmp_path / "run" / "target_base.tfvars.json"
    var_file.parent.mkdir()
    var_file.write_text("{}")
    state = tmp_path / "run" / "target_base.tfstate"
    return module, var_file, state


def _runner(paths, fake):
    module, var_file, state = paths
    return terraform.Terraform(
        working_dir=module, var_file=var_file, state_path=state, run=fake
    )


def test_init_points_local_backend_at_state_path(paths):
    fake = FakeRun()

    _runner(paths, fake).init()

    ((args, env),) = fake.calls
    module, _, state = paths
    assert args[:3] == ["terraform", f"-chdir={module}", "init"]
    assert f"-backend-config=path={state}" in args
    assert "-input=false" in args
    assert env["TF_DATA_DIR"] == str(
        state.parent / "target_base.tfstate.terraform"
    )
    assert env["TF_IN_AUTOMATION"] == "1"


def test_relative_paths_are_made_absolute(paths, monkeypatch):
    module, var_file, state = paths
    monkeypatch.chdir(module.parent)
    fake = FakeRun({"show": _ok("{}")})
    runner = terraform.Terraform(
        working_dir=pathlib.Path("module"),
        var_file=pathlib.Path("run/target_base.tfvars.json"),
        state_path=pathlib.Path("run/target_base.tfstate"),
        run=fake,
    )

    runner.init()
    runner.plan()

    init_args, plan_args = fake.calls[0][0], fake.calls[1][0]
    assert f"-chdir={module}" in init_args
    assert f"-backend-config=path={state}" in init_args
    assert f"-var-file={var_file}" in plan_args


def test_plan_returns_only_addresses_types_and_actions(paths):
    show = {
        "variables": {"db_password": {"value": _CANARY}},
        "resource_changes": [
            {
                "address": "google_sql_database_instance.db",
                "type": "google_sql_database_instance",
                "change": {"actions": ["create"], "after": {"x": _CANARY}},
            },
            {
                "address": 'google_secret_manager_secret.this["db-url"]',
                "type": "google_secret_manager_secret",
                "change": {"actions": ["delete", "create"]},
            },
        ],
    }
    fake = FakeRun({"show": _ok(json.dumps(show))})

    changes = _runner(paths, fake).plan()

    assert changes == (
        terraform.ResourceChange(
            address="google_sql_database_instance.db",
            resource_type="google_sql_database_instance",
            actions=("create",),
        ),
        terraform.ResourceChange(
            address='google_secret_manager_secret.this["db-url"]',
            resource_type="google_secret_manager_secret",
            actions=("delete", "create"),
        ),
    )
    assert _CANARY not in repr(changes)
    plan_args = fake.calls[0][0]
    _, var_file, state = paths
    assert f"-var-file={var_file}" in plan_args
    assert f"-out={state.parent / 'target_base.tfstate.tfplan'}" in plan_args
    show_args = fake.calls[1][0]
    assert show_args[2:] == [
        "show",
        "-json",
        "-no-color",
        str(state.parent / "target_base.tfstate.tfplan"),
    ]


def test_plan_with_no_changes_returns_empty_tuple(paths):
    fake = FakeRun({"show": _ok(json.dumps({"format_version": "1.2"}))})

    assert _runner(paths, fake).plan() == ()


def test_plan_rejects_unreadable_show_output_without_echoing_it(paths):
    fake = FakeRun({"show": _ok(f"not json {_CANARY}")})

    with pytest.raises(terraform.TerraformError) as excinfo:
        _runner(paths, fake).plan()

    assert _CANARY not in str(excinfo.value)


def test_failed_command_error_carries_no_stdout_or_stderr_text(paths):
    stderr = f"Error: googleapi: 400 password={_CANARY}\nline 2\nline 3"
    fake = FakeRun({"plan": _fail(stderr)})

    with pytest.raises(terraform.TerraformError) as excinfo:
        _runner(paths, fake).plan()

    error = excinfo.value
    assert error.subcommand == "plan"
    assert error.returncode == 1
    assert "3줄" in str(error)
    assert _CANARY not in str(error)
    assert _CANARY not in repr(error)
    assert error.__cause__ is None and error.__context__ is None
    assert fake.subcommands() == ["plan"]


def test_inherited_terraform_env_cannot_redirect_the_run(paths, monkeypatch):
    monkeypatch.setenv("TF_WORKSPACE", "dev")
    monkeypatch.setenv("TF_CLI_ARGS_plan", "-state=/elsewhere.tfstate")
    monkeypatch.setenv("TF_LOG_PATH", "/tmp/r2g_trace.log")
    monkeypatch.setenv("TF_VAR_project_id", "someone-else")
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", "/cache")
    monkeypatch.setenv("TF_TOKEN_app_terraform_io", "registry-token")
    monkeypatch.setenv("CLOUDSDK_CORE_PROJECT", "kept")
    fake = FakeRun()

    _runner(paths, fake).init()

    ((_, env),) = fake.calls
    for name in ("TF_WORKSPACE", "TF_CLI_ARGS_plan", "TF_LOG_PATH"):
        assert name not in env
    assert "TF_VAR_project_id" not in env
    assert env["TF_PLUGIN_CACHE_DIR"] == "/cache"
    assert env["TF_TOKEN_app_terraform_io"] == "registry-token"
    assert env["CLOUDSDK_CORE_PROJECT"] == "kept"


def test_state_paths_differing_only_in_suffix_do_not_share_files(tmp_path):
    fakes = (FakeRun({"show": _ok("{}")}), FakeRun({"show": _ok("{}")}))
    for state_name, fake in zip(
        ("terraform.r1", "terraform.r2"), fakes, strict=True
    ):
        terraform.Terraform(
            working_dir=tmp_path,
            var_file=tmp_path / "v.tfvars.json",
            state_path=tmp_path / state_name,
            run=fake,
        ).plan()

    (_, env1), (_, env2) = fakes[0].calls[0], fakes[1].calls[0]
    assert env1["TF_DATA_DIR"] != env2["TF_DATA_DIR"]
    assert (tmp_path / "terraform.r1.tfplan").exists()
    assert (tmp_path / "terraform.r2.tfplan").exists()


def test_plan_passes_unchanged_resources_through_as_no_op(paths):
    show = {
        "resource_changes": [
            {
                "address": "google_sql_database.app",
                "type": "google_sql_database",
                "change": {"actions": ["no-op"]},
            }
        ]
    }
    fake = FakeRun({"show": _ok(json.dumps(show))})

    changes = _runner(paths, fake).plan()

    assert changes == (
        terraform.ResourceChange(
            address="google_sql_database.app",
            resource_type="google_sql_database",
            actions=("no-op",),
        ),
    )


@pytest.mark.parametrize(
    "results",
    [
        {"show": _fail("Error: plan file corrupt")},
        {"show": _ok("not json")},
    ],
)
def test_plan_removes_saved_plan_when_reading_it_fails(paths, results):
    fake = FakeRun(results)

    with pytest.raises(terraform.TerraformError):
        _runner(paths, fake).plan()

    assert not (paths[2].parent / "target_base.tfstate.tfplan").exists()


def test_failed_plan_removes_stale_plan_from_earlier_run(paths):
    stale = paths[2].parent / "target_base.tfstate.tfplan"
    stale.write_text(_CANARY)
    fake = FakeRun({"plan": _fail("Error: x")})

    with pytest.raises(terraform.TerraformError):
        _runner(paths, fake).plan()

    assert not stale.exists()


def test_apply_without_saved_plan_is_refused(paths):
    fake = FakeRun()

    with pytest.raises(terraform.TerraformError, match="plan"):
        _runner(paths, fake).apply()

    assert fake.calls == []


def test_apply_applies_saved_plan_and_removes_it(paths):
    fake = FakeRun({"show": _ok("{}")})
    runner = _runner(paths, fake)
    runner.plan()
    plan_file = paths[2].parent / "target_base.tfstate.tfplan"
    assert plan_file.exists()

    runner.apply()

    apply_args = fake.calls[-1][0]
    assert apply_args[2:] == [
        "apply",
        "-input=false",
        "-no-color",
        str(plan_file),
    ]
    assert not any(a.startswith("-var-file") for a in apply_args)
    assert not plan_file.exists()


def test_apply_failure_still_removes_saved_plan(paths):
    fake = FakeRun({"show": _ok("{}"), "apply": _fail("Error: quota")})
    runner = _runner(paths, fake)
    runner.plan()

    with pytest.raises(terraform.TerraformError, match="종료 코드 1"):
        runner.apply()

    assert not (paths[2].parent / "target_base.tfstate.tfplan").exists()


def test_destroy_returns_addresses_left_in_state(paths):
    fake = FakeRun({"state": _ok("google_sql_database_instance.db\n")})

    remaining = _runner(paths, fake).destroy()

    assert remaining == ("google_sql_database_instance.db",)
    destroy_args, state_args = fake.calls[0][0], fake.calls[1][0]
    _, var_file, _ = paths
    assert destroy_args[2:5] == ["destroy", "-auto-approve", "-input=false"]
    assert f"-var-file={var_file}" in destroy_args
    assert state_args[2:] == ["state", "list"]


def test_destroy_of_empty_state_returns_empty_tuple(paths):
    fake = FakeRun({"state": _ok("")})

    assert _runner(paths, fake).destroy() == ()


def test_outputs_drop_sensitive_values(paths):
    output = {
        "service_uri": {"sensitive": False, "type": "string", "value": "u"},
        "admin_url": {"sensitive": True, "type": "string", "value": _CANARY},
    }
    fake = FakeRun({"output": _ok(json.dumps(output))})

    outputs = _runner(paths, fake).outputs()

    assert outputs == {"service_uri": "u"}
    assert fake.calls[0][0][2:] == ["output", "-json", "-no-color"]


def test_outputs_reject_unreadable_json_without_echoing_it(paths):
    fake = FakeRun({"output": _ok(f"{{broken {_CANARY}")})

    with pytest.raises(terraform.TerraformError) as excinfo:
        _runner(paths, fake).outputs()

    assert _CANARY not in str(excinfo.value)


def test_outputs_reject_json_that_is_not_an_object(paths):
    fake = FakeRun({"output": _ok("[]")})

    with pytest.raises(terraform.TerraformError, match="객체"):
        _runner(paths, fake).outputs()


def test_plan_removes_saved_plan_when_show_cannot_even_start(paths):
    fake = FakeRun()

    def run(args, **kwargs):
        if args[2] == "show":
            raise OSError("terraform binary vanished")
        return fake(args, **kwargs)

    runner = terraform.Terraform(
        working_dir=paths[0], var_file=paths[1], state_path=paths[2], run=run
    )

    with pytest.raises(OSError):
        runner.plan()

    assert not (paths[2].parent / "target_base.tfstate.tfplan").exists()


@pytest.mark.parametrize(
    "changes",
    [
        [{"type": "t", "change": {"actions": ["create"]}}],
        [{"address": "a", "type": "t", "change": {}}],
        ["not-an-object"],
        {"address": "a"},
    ],
)
def test_plan_rejects_malformed_resource_changes_and_removes_plan(
    paths, changes
):
    show = json.dumps({"resource_changes": changes, "secret": _CANARY})
    fake = FakeRun({"show": _ok(show)})

    with pytest.raises(terraform.TerraformError) as excinfo:
        _runner(paths, fake).plan()

    assert _CANARY not in str(excinfo.value)
    assert not (paths[2].parent / "target_base.tfstate.tfplan").exists()
