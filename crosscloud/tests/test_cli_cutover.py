import fcntl
import json
import socket

import fake_ingress
import pytest

from crosscloud import cli


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _write_handoff(tmp_path, port):
    cloud = tmp_path / "config.cloud.yaml"
    cloud.write_text(
        "targets: {gcp: {dbname: shop_b}}\n"
        "job: {job_id: j, source: aws, target: gcp}\n"
        f"application: {{control_url: 'http://127.0.0.1:{port}'}}\n"
    )
    env = tmp_path / "ingress.env"
    env.write_text(
        'INGRESS_TARGETS={"aws":{"url":"http://a"},"gcp":{"url":"http://g"},'
        '"onprem":{"url":"http://a"}}\n'
    )
    return cloud, env


def _args(command, cloud, env, url):
    return [
        command,
        "--cloud-config",
        str(cloud),
        "--ingress-env",
        str(env),
        "--ingress-control-url",
        url,
    ]


def _start_args(cloud, env, url, state_dir, *extra):
    return [
        *_args("ingress-start", cloud, env, url),
        "--state-dir",
        str(state_dir),
        *extra,
    ]


def _state_dir(tmp_path, migration_module, stage=None, **checkpoint):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if stage is None:
        return state_dir
    state = migration_module("migration.state")
    store = state.CutoverStore(state_dir)
    try:
        store.save(
            state.CutoverJob(
                "j", "aws->gcp", "aws", "gcp", stage, checkpoint, ""
            )
        )
    finally:
        store.close()
    return state_dir


def test_ingress_start_points_queue_at_source(
    tmp_path, capsys, monkeypatch, migration_module
):
    monkeypatch.setenv("INGRESS_CONTROL_TOKEN", "t" * 40)
    fake = fake_ingress.FakeIngress(token="t" * 40)
    cloud, env = _write_handoff(tmp_path, _free_port())
    state_dir = _state_dir(tmp_path, migration_module, "READY")
    with fake.serve() as url:
        code = cli.main(_start_args(cloud, env, url, state_dir))

    assert code == 0
    assert json.loads(capsys.readouterr().out)["target"] == "aws"
    assert fake.state["mode"] == "running"


def test_ingress_start_failure_exits_nonzero(
    tmp_path, capsys, monkeypatch, migration_module
):
    monkeypatch.delenv("INGRESS_CONTROL_TOKEN", raising=False)
    fake = fake_ingress.FakeIngress()
    fake.state.update(target="gcp", epoch="2")
    cloud, env = _write_handoff(tmp_path, _free_port())
    state_dir = _state_dir(tmp_path, migration_module, "COPYING")
    with fake.serve() as url:
        code = cli.main(_start_args(cloud, env, url, state_dir))

    assert code == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_build_adapter_server_listens_on_control_url_port(tmp_path):
    port = _free_port()
    cloud, env = _write_handoff(tmp_path, port)
    parser = cli.build_parser()
    args = parser.parse_args(
        _args("serve-adapter", cloud, env, "http://127.0.0.1:9")
    )

    server = cli.build_server(args)
    try:
        assert server.server_address == ("127.0.0.1", port)
    finally:
        server.server_close()


def test_unknown_command_is_usage_error():
    with pytest.raises(SystemExit):
        cli.main(["shift-traffic"])


@pytest.mark.parametrize(
    ("stage", "checkpoint"),
    [
        ("FENCING", {}),
        ("COMPLETED", {}),
        ("NEEDS_RECOVERY", {}),
        ("FAILED", {"failed_stage": "FENCING"}),
    ],
)
def test_ingress_start_refused_after_approval(
    tmp_path, capsys, migration_module, stage, checkpoint
):
    fake = fake_ingress.FakeIngress()
    cloud, env = _write_handoff(tmp_path, _free_port())
    state_dir = _state_dir(tmp_path, migration_module, stage, **checkpoint)
    with fake.serve() as url:
        code = cli.main(_start_args(cloud, env, url, state_dir))

    assert code == 1
    assert stage in json.loads(capsys.readouterr().out)["error"]
    assert fake.calls == []


def test_ingress_start_needs_explicit_flag_without_state_file(
    tmp_path, capsys, migration_module
):
    fake = fake_ingress.FakeIngress()
    cloud, env = _write_handoff(tmp_path, _free_port())
    state_dir = _state_dir(tmp_path, migration_module)
    with fake.serve() as url:
        refused = cli.main(_start_args(cloud, env, url, state_dir))
        refused_out = json.loads(capsys.readouterr().out)
        allowed = cli.main(
            _start_args(cloud, env, url, state_dir, "--allow-no-state")
        )

    assert refused == 1
    assert "--allow-no-state" in refused_out["error"]
    assert allowed == 0
    assert fake.state["target"] == "aws"


def test_ingress_start_refused_while_migration_cloud_runs(
    tmp_path, capsys, migration_module
):
    fake = fake_ingress.FakeIngress()
    cloud, env = _write_handoff(tmp_path, _free_port())
    state_dir = _state_dir(tmp_path, migration_module, "READY")
    with (state_dir / "cloud.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with fake.serve() as url:
            code = cli.main(_start_args(cloud, env, url, state_dir))

    assert code == 1
    assert "cloud.lock" in json.loads(capsys.readouterr().out)["error"]
    assert fake.calls == []


def test_ingress_start_refuses_config_without_job_id(
    tmp_path, capsys, migration_module
):
    fake = fake_ingress.FakeIngress()
    cloud, env = _write_handoff(tmp_path, _free_port())
    cloud.write_text("job: {}\n")
    state_dir = _state_dir(tmp_path, migration_module, "READY")
    with fake.serve() as url:
        code = cli.main(_start_args(cloud, env, url, state_dir))

    assert code == 1
    assert "job_id" in json.loads(capsys.readouterr().out)["error"]
    assert fake.calls == []


def test_ingress_start_refuses_unreadable_state(
    tmp_path, capsys, migration_module
):
    fake = fake_ingress.FakeIngress()
    cloud, env = _write_handoff(tmp_path, _free_port())
    state_dir = _state_dir(tmp_path, migration_module)
    (state_dir / "cutover.sqlite3").write_text("broken")
    with fake.serve() as url:
        code = cli.main(_start_args(cloud, env, url, state_dir))

    assert code == 1
    assert fake.calls == []
