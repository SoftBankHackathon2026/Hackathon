"""prepare-target · ingress-status · ingress-drain-check · render-handoff."""

import json

import fake_ingress
import fake_redis
import pytest
import yaml

from crosscloud import cli, cutover_cli, handoff, model, target_deploy

_CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def _handoff_files(tmp_path):
    cloud = tmp_path / "config.cloud.yaml"
    cloud.write_text(
        yaml.safe_dump(
            {
                "targets": {
                    "aws": {
                        "host": "src.example",
                        "port": 5432,
                        "dbname": "shop",
                        "sslmode": "require",
                        "users": {},
                    },
                    "gcp": {
                        "host": "34.64.1.2",
                        "port": 5432,
                        "dbname": "shop_b",
                        "sslmode": "require",
                        "users": {
                            "admin": {
                                "user": "migration_admin",
                                "password_env": "GCP_ADMIN_PASSWORD",
                            },
                            "app": {
                                "user": "shop_app",
                                "password_env": "GCP_APP_PASSWORD",
                            },
                        },
                    },
                },
                "job": {"job_id": "j", "source": "aws", "target": "gcp"},
                "application": {"control_url": "http://127.0.0.1:18080"},
            }
        )
    )
    env = tmp_path / "ingress.env"
    env.write_text(
        'INGRESS_TARGETS={"aws":{"url":"http://a"},"gcp":{"url":"http://g"}}\n'
    )
    deploy = tmp_path / "target-deploy.yaml"
    deploy.write_text(
        "platform: cloud_run\n"
        "service: projects/p/locations/r/services/ccx-app\n"
        "secret_env:\n"
        "  DATABASE_URL: {secret: ccx-target-profile, version: '2'}\n"
    )
    ca = tmp_path / "ca.pem"
    ca.write_text(_CA)
    return cloud, env, deploy, ca


def _state(tmp_path, migration_module, stage):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    if stage is not None:
        state = migration_module("migration.state")
        store = state.CutoverStore(state_dir)
        try:
            store.save(
                state.CutoverJob("j", "aws->gcp", "aws", "gcp", stage, {}, "")
            )
        finally:
            store.close()
    return state_dir


class _Fakes:
    def __init__(self, identity_problems=()):
        self.logins = []
        self.deployed = []
        self.identity_problems = identity_problems

    def open_app_login(self, login, app_user):
        self.logins.append(
            (login.endpoint, login.user, login.password, app_user)
        )

    def make_deployer(self, spec):
        fakes = self

        class _Deployer:
            def deploy(self, profile):
                fakes.deployed.append((spec, profile))
                return "rev-2"

        return _Deployer()

    def inspect_app(self, base_url, *, database, environment, timeout):
        return self.identity_problems

    def deps(self, env=None):
        return cutover_cli.Deps(
            open_app_login=self.open_app_login,
            make_deployer=self.make_deployer,
            inspect_app=self.inspect_app,
            environ=env if env is not None else {"GCP_ADMIN_PASSWORD": "pw"},
            sleep=self._sleep,
            clock=lambda: self.now,
        )

    now = 0.0

    def _sleep(self, seconds):
        self.now += seconds


def _prepare_args(cloud, env, deploy, ca, state_dir):
    return cli.build_parser().parse_args(
        [
            "prepare-target",
            "--cloud-config",
            str(cloud),
            "--ingress-env",
            str(env),
            "--state-dir",
            str(state_dir),
            "--deploy",
            str(deploy),
            "--db-ca-file",
            str(ca),
        ]
    )


def test_prepare_target_opens_login_redeploys_and_checks_identity(
    tmp_path, capsys, migration_module
):
    cloud, env, deploy, ca = _handoff_files(tmp_path)
    fakes = _Fakes()
    state_dir = _state(tmp_path, migration_module, "READY")

    code = cutover_cli.run(
        _prepare_args(cloud, env, deploy, ca, state_dir), fakes.deps()
    )

    assert code == 0
    ((endpoint, admin, password, app_user),) = fakes.logins
    assert (endpoint.host, endpoint.database) == ("34.64.1.2", "shop_b")
    assert (admin, password, app_user) == ("migration_admin", b"pw", "shop_app")
    ((spec, profile),) = fakes.deployed
    assert spec.secret_env == (
        target_deploy.SecretEnv("DATABASE_URL", "ccx-target-profile", "2"),
    )
    assert profile.plain_env == (
        ("PGSSLMODE", "verify-full"),
        ("DB_SSL_CA", _CA),
    )
    out = json.loads(capsys.readouterr().out)
    assert out["revision"] == "rev-2"
    assert "pw" not in json.dumps(out)


@pytest.mark.parametrize("stage", ["COPYING", "FENCING", "COMPLETED", None])
def test_prepare_target_only_at_ready(
    tmp_path, capsys, migration_module, stage
):
    cloud, env, deploy, ca = _handoff_files(tmp_path)
    fakes = _Fakes()
    state_dir = _state(tmp_path, migration_module, stage)

    code = cutover_cli.run(
        _prepare_args(cloud, env, deploy, ca, state_dir), fakes.deps()
    )

    assert code == 1
    assert fakes.logins == []
    assert fakes.deployed == []


def test_prepare_target_needs_admin_password_env(
    tmp_path, capsys, migration_module
):
    cloud, env, deploy, ca = _handoff_files(tmp_path)
    fakes = _Fakes()
    state_dir = _state(tmp_path, migration_module, "READY")

    code = cutover_cli.run(
        _prepare_args(cloud, env, deploy, ca, state_dir), fakes.deps(env={})
    )

    assert code == 1
    assert "GCP_ADMIN_PASSWORD" in json.loads(capsys.readouterr().out)["error"]
    assert fakes.deployed == []


def test_prepare_target_fails_when_app_not_on_target_db(
    tmp_path, capsys, migration_module
):
    cloud, env, deploy, ca = _handoff_files(tmp_path)
    fakes = _Fakes(identity_problems=("database=shop (기대 shop_b)",))
    state_dir = _state(tmp_path, migration_module, "READY")

    code = cutover_cli.run(
        _prepare_args(cloud, env, deploy, ca, state_dir), fakes.deps()
    )

    assert code == 1
    assert "database=shop" in json.loads(capsys.readouterr().out)["error"]


def _status_args(cloud, env, url, state_dir):
    return cli.build_parser().parse_args(
        [
            "ingress-status",
            "--cloud-config",
            str(cloud),
            "--ingress-env",
            str(env),
            "--ingress-control-url",
            url,
            "--state-dir",
            str(state_dir),
        ]
    )


def test_ingress_status_ready_to_approve(tmp_path, capsys, migration_module):
    cloud, env, _, _ = _handoff_files(tmp_path)
    state_dir = _state(tmp_path, migration_module, "READY")
    fake = fake_ingress.FakeIngress()
    fake.state.update(mode="running", target="aws", epoch="1")
    with fake.serve() as url:
        code = cutover_cli.run(
            _status_args(cloud, env, url, state_dir), _Fakes().deps()
        )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ready_to_approve"] is True
    assert out["problems"] == []
    assert [c for _, c, _ in fake.calls if c != "/status"] == []


@pytest.mark.parametrize(
    ("stage", "ingress_state", "identity", "word"),
    [
        ("COPYING", {}, (), "COPYING"),
        ("READY", {"mode": "paused"}, (), "paused"),
        ("READY", {"target": "gcp"}, (), "gcp"),
        ("READY", {"last_error": "DELIVERY_RETRY"}, (), "DELIVERY_RETRY"),
        ("READY", {}, ("database=shop (기대 shop_b)",), "database"),
    ],
)
def test_ingress_status_lists_what_blocks_approval(
    tmp_path, capsys, migration_module, stage, ingress_state, identity, word
):
    cloud, env, _, _ = _handoff_files(tmp_path)
    state_dir = _state(tmp_path, migration_module, stage)
    fake = fake_ingress.FakeIngress()
    fake.state.update(mode="running", target="aws", epoch="1")
    fake.state.update(ingress_state)
    with fake.serve() as url:
        code = cutover_cli.run(
            _status_args(cloud, env, url, state_dir), _Fakes(identity).deps()
        )

    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["ready_to_approve"] is False
    assert any(word in p for p in out["problems"])


def test_ingress_drain_check_exit_codes(capsys):
    fake = fake_ingress.FakeIngress()
    group = (
        b"*1\r\n*12\r\n$4\r\nname\r\n$8\r\ndelivery\r\n$9\r\nconsumers\r\n:1\r\n"
        b"$7\r\npending\r\n:0\r\n$17\r\nlast-delivered-id\r\n$3\r\n1-0\r\n"
        b"$12\r\nentries-read\r\n:1\r\n$3\r\nlag\r\n:0\r\n"
    )
    with fake_redis.serve(group) as (redis_url, _):
        with fake.serve() as url:
            drained = cli.main(
                [
                    "ingress-drain-check",
                    "--ingress-control-url",
                    url,
                    "--redis-url",
                    redis_url,
                ]
            )
            fake.state["in_flight"] = 1
            blocked = cli.main(
                [
                    "ingress-drain-check",
                    "--ingress-control-url",
                    url,
                    "--redis-url",
                    redis_url,
                ]
            )

    first, second = capsys.readouterr().out.splitlines()
    assert (drained, json.loads(first)["drained"]) == (0, True)
    assert (blocked, json.loads(second)["drained"]) == (1, False)


def _tf_file(path, outputs):
    path.write_text(
        json.dumps(
            {
                k: {"value": v, "type": "x", "sensitive": False}
                for k, v in outputs.items()
            }
        )
    )
    return path


def _inventory_file(path):
    inventory = model.Inventory(
        cloud="aws",
        collected_at=__import__("datetime").datetime(
            2026, 11, 7, tzinfo=__import__("datetime").UTC
        ),
        resources=(
            model.Resource(
                "postgres_instance",
                "aws",
                "arn:aws:rds:r:1:db:app",
                "r",
                (),
                "api",
            ),
        ),
        workloads=(),
        grants=(),
        restrictions=(),
        restriction_scans=(),
        couplings=(),
        omissions=(),
        attestation=model.CollectionAttestation(None, False, False, 0, "x"),
    )
    from crosscloud import inventory_json

    path.write_text(
        json.dumps({"inventory": inventory_json.inventory_to_json(inventory)})
    )
    return path


def _render_args(tmp_path, readiness):
    inventory = _inventory_file(tmp_path / "discover.json")
    source_tf = _tf_file(
        tmp_path / "src.json",
        {
            "db": {
                "identifier": "app",
                "address": "src.example",
                "port": 5432,
                "name": "shop",
            }
        },
    )
    target_tf = _tf_file(
        tmp_path / "tgt.json",
        {
            "db_instance_name": "db",
            "db_public_ip": "34.64.1.2",
            "database_name": "shop",
        },
    )
    readiness_file = tmp_path / "readiness.json"
    readiness_file.write_text(json.dumps(readiness))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    return [
        "render-handoff",
        "--inventory", str(inventory),
        "--source-tf", str(source_tf),
        "--target-tf", str(target_tf),
        "--target-database", "shop_b",
        "--source-app-user", "shop_app",
        "--table", "public.requests=id",
        "--readiness", str(readiness_file),
        "--control-url", "http://127.0.0.1:18080",
        "--connect-url", "http://127.0.0.1:8083",
        "--source-app-url", "http://src-alb.example",
        "--target-app-url", "http://34.111.2.3",
        "--redis-url", "redis://redis:6379",
        "--out-dir", str(out_dir),
    ]  # fmt: skip


def test_render_handoff_writes_both_files(tmp_path, capsys):
    readiness = [
        {"name": n, "ready": True, "detail": "ok"}
        for n in handoff.READINESS_NAMES
    ]

    code = cli.main(_render_args(tmp_path, readiness))

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    config = yaml.safe_load(
        (tmp_path / "out" / "config.cloud.yaml").read_text()
    )
    assert config["targets"]["gcp"]["dbname"] == "shop_b"
    assert config["targets"]["aws"]["users"]["app"]["user"] == "shop_app"
    assert (tmp_path / "out" / "ingress.env").exists()
    assert "INGRESS_CONTROL_TOKEN" in out["secret_envs"]
    assert "AWS_CDC_PASSWORD" in out["secret_envs"]


def test_render_handoff_refuses_unready(tmp_path, capsys):
    readiness = [{"name": "replication_flags", "ready": False, "detail": "off"}]

    code = cli.main(_render_args(tmp_path, readiness))

    assert code == 1
    assert "readiness" in json.loads(capsys.readouterr().out)["error"]
    assert not (tmp_path / "out" / "config.cloud.yaml").exists()


def test_render_handoff_rejects_bad_table_argument(tmp_path, capsys):
    argv = _render_args(
        tmp_path,
        [
            {"name": n, "ready": True, "detail": "ok"}
            for n in handoff.READINESS_NAMES
        ],
    )
    argv[argv.index("public.requests=id")] = "public.requests"

    code = cli.main(argv)

    assert code == 1
    assert "--table" in json.loads(capsys.readouterr().out)["error"]
