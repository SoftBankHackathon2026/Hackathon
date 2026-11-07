import json
import subprocess

import boto3
import psycopg
import pytest
import rehearsal_outputs as tf

from crosscloud.rehearsal import cli, config, runner, steps

AUTOMATED = (
    "preflight",
    "aws-source",
    "gcp-target",
    "prepare-forward",
    "handoff-forward",
    "prepare-return",
    "handoff-return",
    "audit",
    "cleanup",
)


@pytest.fixture
def state(tmp_path):
    rc = cli.main(
        [
            "--state-dir",
            str(tmp_path),
            "init",
            "--run",
            tf.RUN,
            "--source-image",
            tf.IMAGE,
        ]
    )
    assert rc == 0
    return tmp_path


@pytest.fixture
def no_cloud(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"dry-run 이 밖을 불렀다: {args[:1]}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(psycopg, "connect", refuse)


def _save_outputs(state):
    paths = config.StatePaths(state)
    (state / "tf").mkdir(exist_ok=True)
    for stack, outputs in {
        "aws": tf.AWS,
        "aws_return": tf.AWS_RETURN,
        "registry": tf.REGISTRY,
        "target_base": tf.GCP_BASE,
        "target_app": tf.GCP_APP,
    }.items():
        paths.outputs(stack).write_text(json.dumps(outputs))


def test_init_writes_config_without_secrets(state):
    saved = config.load(config.StatePaths(state).config)

    assert saved.run == tf.RUN
    assert saved.source_image == tf.IMAGE


def test_init_refuses_to_overwrite_other_run(state):
    rc = cli.main(
        [
            "--state-dir",
            str(state),
            "init",
            "--run",
            "other",
            "--source-image",
            tf.IMAGE,
        ]
    )

    assert rc == 1


@pytest.mark.parametrize("step", AUTOMATED)
def test_dry_run_prints_plan_without_touching_cloud_or_state(
    state, no_cloud, capsys, step
):
    before = sorted(p.name for p in state.rglob("*"))

    rc = cli.main(["--state-dir", str(state), step, "--dry-run"])

    assert rc == 0
    assert capsys.readouterr().out.strip()
    assert sorted(p.name for p in state.rglob("*")) == before


def test_dry_run_aws_source_lists_apply_copy_seed_apply(
    state, no_cloud, capsys
):
    cli.main(["--state-dir", str(state), "aws-source", "--dry-run"])

    out = capsys.readouterr().out
    order = [
        "terraform apply aws",
        "aws ecr get-login-password",
        "docker buildx imagetools create",
        "원본 DB 시험 데이터",
        "terraform apply aws",
    ]
    position = 0
    for needle in order:
        position = out.index(needle, position) + 1
    assert '"include_app": true' in out


def test_dry_run_uses_saved_outputs_for_handoff(state, no_cloud, capsys):
    _save_outputs(state)

    cli.main(["--state-dir", str(state), "handoff-forward", "--dry-run"])

    out = capsys.readouterr().out
    assert "--target-database shop_b" in out
    assert tf.GCP_APP["service_uri"] in out
    assert "ccx-r1107a-alb-1234567890" in out


def test_dry_run_cleanup_destroys_in_reverse_order(state, no_cloud, capsys):
    cli.main(["--state-dir", str(state), "cleanup", "--dry-run"])

    out = capsys.readouterr().out
    stacks = [
        line.split()[-1]
        for line in out.splitlines()
        if line.startswith("$ terraform destroy")
    ]
    assert stacks == [
        "target_app",
        "audit",
        "aws_return",
        "target_base",
        "registry",
        "aws",
    ]
    assert out.index("ingress-drain-check") < out.index("terraform destroy")


def test_cutover_commands_fill_paths_and_approval(state, capsys):
    _save_outputs(state)

    rc = cli.main(["--state-dir", str(state), "cutover-forward"])

    out = capsys.readouterr().out
    job = state / "forward"
    assert rc == 0
    assert f"--state-dir {job}/migration-state --allow-no-state" in out
    assert "run --approve ccx-r1107a-forward" in out
    assert f"--deploy {job}/target-deploy.yaml" in out
    assert "INGRESS_CONTROL_TOKEN" in out
    assert "--profile bot up -d traffic-bot" in out


def test_cutover_return_restarts_connect_with_return_env(state, capsys):
    rc = cli.main(["--state-dir", str(state), "cutover-return"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "connect.return.compose.yaml" in out
    assert "run --approve ccx-r1107a-return" in out
    assert "--allow-no-state" in out


def test_env_prints_secret_read_commands(state, capsys):
    rc = cli.main(["--state-dir", str(state), "env", "return"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "export GCP_CDC_PASSWORD=" in out
    assert "ccx-r1107ac-aws-sink-password" in out


def test_step_done_marker_skips_rerun_unless_forced(state, capsys):
    calls = []
    step = steps.Step("demo", lambda ctx: calls.append(ctx.cfg.run))
    ctx = steps.Context.open(state, runner.DryRunner(print), dry=False)

    steps.run_step(ctx, step, force=False)
    steps.run_step(ctx, step, force=False)
    steps.run_step(ctx, step, force=True)

    assert calls == [tf.RUN, tf.RUN]
    assert config.StatePaths(state).done("demo").exists()


def test_status_lists_done_steps(state, capsys):
    config.StatePaths(state).done("aws-source").parent.mkdir()
    config.StatePaths(state).done("aws-source").write_text("{}")

    cli.main(["--state-dir", str(state), "status"])

    out = capsys.readouterr().out
    assert "[x] aws-source" in out
    assert "[ ] gcp-target" in out


def test_step_without_previous_outputs_fails_clearly(state, capsys):
    rc = cli.main(["--state-dir", str(state), "handoff-forward"])

    assert rc == 1
    assert "aws" in capsys.readouterr().err


def test_placeholder_formats_nested_output_names():
    value = runner.Placeholder("aws")

    assert f"http://{value['app']['alb_dns_name']}" == (
        "http://<aws.app.alb_dns_name>"
    )


def _completed(code, out=""):
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


def test_real_sh_returns_stdout_and_adds_env(tmp_path):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return _completed(0, "out")

    real = runner.RealRunner(tmp_path, echo=lambda line: None, run=fake_run)

    assert real.sh(["echo"], capture=True, env={"SESSION": "r1"}) == "out"
    assert seen["env"]["SESSION"] == "r1"


def test_real_sh_failure_reports_exit_code_only(tmp_path):
    real = runner.RealRunner(
        tmp_path,
        echo=lambda line: None,
        run=lambda argv, **kwargs: _completed(2, "secret-ish"),
    )

    with pytest.raises(runner.StepError, match="종료 코드 2") as caught:
        real.sh(["aws", "sts"], capture=True)

    assert "secret-ish" not in str(caught.value)


def test_real_destroy_skips_stack_never_applied(tmp_path):
    lines = []
    real = runner.RealRunner(tmp_path, echo=lines.append)

    assert real.destroy("audit", tmp_path) == ()
    assert "state 없음" in lines[0]


def test_real_call_and_write(tmp_path):
    real = runner.RealRunner(tmp_path, echo=lambda line: None)

    assert real.call("t", lambda: 7, 0) == 7
    real.write(tmp_path / "a" / "b.json", "{}")
    assert (tmp_path / "a" / "b.json").read_text() == "{}"


def test_preflight_flags_wrong_account_and_project():
    cfg = config.RehearsalConfig(run=tf.RUN, source_image=tf.IMAGE)

    problems = steps.preflight_problems(
        cfg, json.dumps({"Account": "111"}), "other\n", ["crane"]
    )

    assert problems == [
        "도구 없음: crane",
        "AWS 계정 111 ≠ 187478112557",
        "GCP 프로젝트 other ≠ ghost-project-513812",
    ]


def test_cleanup_refuses_to_report_success_with_residue(state, monkeypatch):
    ctx = steps.Context.open(
        state, runner.DryRunner(lambda line: None), dry=True
    )
    monkeypatch.setattr(steps, "_docker_residue", lambda ctx: ["abc123"])

    with pytest.raises(runner.StepError, match="abc123"):
        steps.cleanup(ctx)
