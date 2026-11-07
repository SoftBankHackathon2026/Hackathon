import json
import logging
import subprocess

import boto3
import psycopg
import pytest
import rehearsal_outputs as tf

from crosscloud import cli as crosscloud_cli
from crosscloud.provision import terraform
from crosscloud.rehearsal import cli, config, runner, steps
from crosscloud.secret_transfer import store

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


class _ScriptedRunner:
    """밖을 부르지 않는 실행기. 명령 앞부분에 맞춰 표준 출력을 돌려준다."""

    def __init__(self, replies=None, fail_destroy=()):
        self.replies = {
            "aws sts get-caller-identity": json.dumps({"Account": tf.ACCOUNT}),
            **(replies or {}),
        }
        self.fail_destroy = set(fail_destroy)
        self.log = []

    def sh(self, argv, *, capture=False, env=None):
        line = " ".join(argv)
        self.log.append(line)
        for prefix, out in self.replies.items():
            if not line.startswith(prefix):
                continue
            if isinstance(out, list):  # 차례로 돌려주고 마지막 것을 유지한다.
                return out.pop(0) if len(out) > 1 else out[0]
            return out
        return ""

    def pipe(self, producer, consumer):
        self.log.append(" ".join(producer) + " | " + " ".join(consumer))

    def call(self, title, action, placeholder):
        self.log.append("· " + title)
        return placeholder

    def apply(self, stack, module, tfvars):
        self.log.append(f"apply {stack}")
        return {}

    def destroy(self, stack, module):
        self.log.append(f"destroy {stack}")
        if stack in self.fail_destroy:
            raise terraform.TerraformError("destroy", 1, "보존 기간")
        return ()

    def write(self, path, text):
        self.log.append(f"write {path.name}")


_STARTED = "docker ps -a -q --filter label=com.ghost.crosscloud.session"
_PORT_OWNER = "docker ps --filter publish="


def _real_ctx(state, scripted):
    return steps.Context.open(state, scripted, dry=False, echo=lambda _: None)


def _destroyed(scripted):
    return [
        line.split()[1] for line in scripted.log if line.startswith("destroy")
    ]


def test_cleanup_refuses_drain_check_on_other_sessions_ports(state):
    scripted = _ScriptedRunner({_STARTED: "c1\n", _PORT_OWNER: "otherrun\n"})

    with pytest.raises(runner.StepError, match="otherrun"):
        steps.cleanup(_real_ctx(state, scripted))

    assert not any("ingress-drain-check" in line for line in scripted.log)
    assert not any("down -v" in line for line in scripted.log)
    assert _destroyed(scripted) == []


def test_cleanup_drains_own_session_then_downs_compose(state):
    scripted = _ScriptedRunner(
        {_STARTED: ["c1\n", ""], _PORT_OWNER: tf.RUN + "\n"}
    )

    steps.cleanup(_real_ctx(state, scripted))

    drain = next(
        i
        for i, line in enumerate(scripted.log)
        if "ingress-drain-check" in line
    )
    down = next(i for i, line in enumerate(scripted.log) if "down -v" in line)
    assert drain < down
    assert len(_destroyed(scripted)) == 6


def test_cleanup_skips_drain_when_compose_never_started(state):
    ingress_env = config.StatePaths(state).job("forward").out / "ingress.env"
    ingress_env.parent.mkdir(parents=True)
    ingress_env.write_text("TARGET=x\n")
    scripted = _ScriptedRunner()

    steps.cleanup(_real_ctx(state, scripted))

    assert not any("ingress-drain-check" in line for line in scripted.log)
    assert _destroyed(scripted) == list(steps.DESTROY_ORDER)


def test_cleanup_keeps_destroying_after_one_stack_fails(state):
    _save_outputs(state)
    scripted = _ScriptedRunner(fail_destroy={"audit"})

    with pytest.raises(runner.StepError, match="audit") as caught:
        steps.cleanup(_real_ctx(state, scripted))

    assert _destroyed(scripted) == list(steps.DESTROY_ORDER)
    assert any("잔여 질의" in line for line in scripted.log)
    assert sum(line.startswith("docker logout") for line in scripted.log) == 2
    assert "보존 기간" in str(caught.value)


def test_run_step_refuses_wrong_aws_account(state):
    calls = []
    scripted = _ScriptedRunner(
        {"aws sts get-caller-identity": json.dumps({"Account": "111"})}
    )

    with pytest.raises(runner.StepError, match="111"):
        steps.run_step(
            _real_ctx(state, scripted),
            steps.Step("demo", calls.append),
            force=False,
        )

    assert calls == []


def test_forced_rerun_failure_leaves_step_not_done(state):
    def broken(ctx):
        raise runner.StepError("이미지 복사 실패")

    ctx = _real_ctx(state, _ScriptedRunner())
    steps.run_step(ctx, steps.Step("demo", lambda ctx: None), force=False)

    with pytest.raises(runner.StepError):
        steps.run_step(ctx, steps.Step("demo", broken), force=True)

    assert not config.StatePaths(state).done("demo").exists()


def test_handoff_stops_when_discover_needs_human_decision(state, monkeypatch):
    _save_outputs(state)
    paths = config.StatePaths(state)
    paths.values.write_text(json.dumps({"profile_b_version": "2"}))
    monkeypatch.setattr(crosscloud_cli, "main", lambda argv: 3)
    ctx = steps.Context.open(
        state, runner.RealRunner(state, echo=lambda _: None), dry=False
    )

    with pytest.raises(runner.StepError, match="종료 코드 3"):
        steps.handoff_forward(ctx)

    assert not paths.job("forward").deploy.exists()


def test_aws_master_refuses_sdk_debug_logging_before_reading_secret(
    state, monkeypatch
):
    reads = []

    class FakeSecrets:
        def get_secret_value(self, **kwargs):
            reads.append(kwargs)
            return {"SecretString": "{}"}

    monkeypatch.setattr(boto3, "client", lambda *a, **k: FakeSecrets())
    botocore_logger = logging.getLogger("botocore")
    before = botocore_logger.level
    ctx = _real_ctx(state, _ScriptedRunner())

    # setLevel 로 바꾸고 되돌린다 — 수준 캐시를 비워 다른 시험에 새지 않게.
    botocore_logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(store.UnsafeLoggingError):
            steps._aws_master(ctx, tf.AWS, "shop")
    finally:
        botocore_logger.setLevel(before)

    assert reads == []


def test_dry_run_preflight_reports_missing_tools_without_stopping(
    state, no_cloud, capsys, monkeypatch
):
    monkeypatch.setattr(steps.shutil, "which", lambda tool: None)

    rc = cli.main(["--state-dir", str(state), "preflight", "--dry-run"])

    assert rc == 0
    assert "도구 없음: terraform" in capsys.readouterr().out


def test_real_preflight_stops_on_missing_tool(state, monkeypatch):
    monkeypatch.setattr(steps.shutil, "which", lambda tool: None)
    scripted = _ScriptedRunner(
        {"gcloud config get-value project": tf.PROJECT + "\n"}
    )

    with pytest.raises(runner.StepError, match="도구 없음: terraform"):
        steps.preflight(_real_ctx(state, scripted))


def test_cleanup_stops_and_downs_bot_profile_too(state):
    scripted = _ScriptedRunner(
        {_STARTED: ["c1\n", ""], _PORT_OWNER: tf.RUN + "\n"}
    )

    steps.cleanup(_real_ctx(state, scripted))

    ingress = [line for line in scripted.log if "ingress.compose" in line]
    assert ingress
    assert all("--profile bot" in line for line in ingress)


def test_cleanup_deregisters_task_definitions_after_destroy(state):
    scripted = _ScriptedRunner()

    steps.cleanup(_real_ctx(state, scripted))

    deregister = next(
        i for i, line in enumerate(scripted.log) if "ccx-r1107a-app" in line
    )
    assert deregister > scripted.log.index("destroy aws")


class _FakeEcs:
    def __init__(self, arns):
        self.arns = arns
        self.deregistered = []
        self.listed = []

    def get_paginator(self, name):
        assert name == "list_task_definitions"
        return self

    def paginate(self, **kwargs):
        self.listed.append(kwargs)
        return [{"taskDefinitionArns": self.arns}]

    def deregister_task_definition(self, **kwargs):
        self.deregistered.append(kwargs["taskDefinition"])


def test_deregister_task_definitions_only_touches_exact_family():
    arn = "arn:aws:ecs:ap-northeast-2:187478112557:task-definition/"
    ecs = _FakeEcs(
        [
            arn + "ccx-r1107a-app:1",
            arn + "ccx-r1107a-app:2",
            arn + "ccx-r1107a-app2:1",
        ]
    )

    left = steps.deregister_task_definitions(ecs, "ccx-r1107a-app")

    assert ecs.listed == [
        {"familyPrefix": "ccx-r1107a-app", "status": "ACTIVE"}
    ]
    assert ecs.deregistered == [
        arn + "ccx-r1107a-app:1",
        arn + "ccx-r1107a-app:2",
    ]
    assert left == ecs.deregistered
