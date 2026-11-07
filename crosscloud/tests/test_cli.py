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


def test_ingress_start_points_queue_at_source(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("INGRESS_CONTROL_TOKEN", "t" * 40)
    fake = fake_ingress.FakeIngress(token="t" * 40)
    cloud, env = _write_handoff(tmp_path, _free_port())
    with fake.serve() as url:
        code = cli.main(_args("ingress-start", cloud, env, url))

    assert code == 0
    assert json.loads(capsys.readouterr().out)["target"] == "aws"
    assert fake.state["mode"] == "running"


def test_ingress_start_failure_exits_nonzero(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("INGRESS_CONTROL_TOKEN", raising=False)
    fake = fake_ingress.FakeIngress()
    fake.state.update(target="gcp", epoch="2")
    cloud, env = _write_handoff(tmp_path, _free_port())
    with fake.serve() as url:
        code = cli.main(_args("ingress-start", cloud, env, url))

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
