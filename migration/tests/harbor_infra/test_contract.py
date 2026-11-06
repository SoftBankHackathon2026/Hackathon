import copy
import json
import subprocess
import sys

import pytest
from migration.harbor_infra.config import ConfigError, secret_file, validate
from migration.harbor_infra.runner import plan_digest, run


def test_strict_contract(raw):
    c = validate(raw, "preflight")
    assert c["verification"]["runtime_pull"] is False
    assert c["projects"][0]["adopt_existing"] is False
    assert c["keycloak"]["groups_claim"] == "groups"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "http://registry.example",
        "https://user:password@registry.example",
        "https://registry.example/?secret=1",
        "https://registry.example/#secret",
        "https://registry.example/path",
        "https://registry.example:0",
        "https://registry.example/{{lookup}}",
    ],
)
def test_rejects_unsafe_addresses(raw, bad):
    raw["endpoint"]["url"] = bad
    with pytest.raises(ConfigError):
        validate(raw, "inspect")


@pytest.mark.parametrize(
    "field", ["arbitrary_command", "ansible_playbook", "credentials"]
)
def test_unknown_fields(raw, field):
    raw[field] = "DO_NOT_LEAK"
    with pytest.raises(ConfigError) as exc:
        validate(raw, "inspect")
    assert "DO_NOT_LEAK" not in str(exc.value)


def test_plan_ignores_invocation_but_binds_input_contents(raw):
    c = validate(raw, "preflight")
    first = plan_digest(c)
    c["request_id"] = "another"
    c["plan_digest"] = "0" * 64
    assert plan_digest(c) == first
    with open(c["endpoint"]["admin_password_ref"], "w") as f:
        f.write("new-administrator-secret-for-tests")
    assert plan_digest(c) != first


def test_configure_uses_same_plan_as_preflight(raw):
    c = validate(raw, "preflight")
    raw["plan_digest"] = plan_digest(c)
    assert plan_digest(validate(raw, "configure")) == raw["plan_digest"]


def test_plan_mismatch_never_launches_ansible(raw, monkeypatch):
    raw["plan_digest"] = "0" * 64
    monkeypatch.setattr(
        "migration.harbor_infra.runner.ansible",
        lambda *a: pytest.fail("must not execute"),
    )
    result = run("configure", validate(raw, "configure"))
    assert result["status"] == "blocked" and result["reason_codes"] == ["PLAN_CHANGED"]


def test_secret_permissions_and_symlinks(tmp_path):
    p = tmp_path / "secret"
    p.write_text("private credential")
    p.chmod(0o644)
    with pytest.raises(ConfigError):
        secret_file(str(p))
    p.chmod(0o600)
    assert secret_file(str(p)) == "private credential"
    link = tmp_path / "link"
    link.symlink_to(p)
    with pytest.raises(ConfigError):
        secret_file(str(link))


def test_secret_cannot_be_ansible_template(tmp_path):
    p = tmp_path / "secret"
    p.write_text('{{ lookup("pipe", "bad") }}')
    p.chmod(0o600)
    with pytest.raises(ConfigError):
        secret_file(str(p))


def test_restart_scope_cannot_expand(raw):
    raw["trust"] = {"targets": [], "restart_approved": ["other-node"]}
    with pytest.raises(ConfigError):
        validate(raw, "preflight")


def test_probe_requires_explicit_namespace_and_opt_in(raw):
    raw["verification"] = {"runtime_pull": True}
    with pytest.raises(ConfigError):
        validate(raw, "preflight")


def test_cli_redacts_invalid_values(raw, tmp_path):
    import yaml

    raw["endpoint"]["url"] = "https://secret-user:SECRET-PASSWORD@registry.example"
    file = tmp_path / "config.yaml"
    file.write_text(yaml.safe_dump(raw))
    p = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration.harbor_infra",
            "inspect",
            "--config",
            str(file),
        ],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 3 and "SECRET-PASSWORD" not in p.stdout + p.stderr
    assert json.loads(p.stdout)["status"] == "blocked"


def test_new_node_and_runtime_validation(raw, tmp_path):
    key = tmp_path / "ssh"
    key.write_text("key")
    key.chmod(0o600)
    raw.update(
        target={
            "mode": "new",
            "installation_id": "registry",
            "node_name": "worker.example",
        },
        cluster={"id": "cluster", "kubeconfig_ref": str(key), "context": "test"},
        nodes=[
            {
                "name": "worker.example",
                "uid": "node-uid",
                "ssh_host": "192.0.2.1",
                "ssh_user": "ubuntu",
                "private_key_ref": str(key),
                "known_hosts_ref": str(key),
            }
        ],
        install={
            "data_path": "/srv/harbor/data",
            "mount_path": "/srv/harbor",
            "capacity_gb": 160,
            "database_password_ref": raw["endpoint"]["admin_password_ref"],
        },
        tls={"mode": "self_signed"},
    )
    validate(raw, "preflight")
    for badpath in ["/", "/data", "/srv/../data"]:
        bad = copy.deepcopy(raw)
        bad["install"]["data_path"] = badpath
        with pytest.raises(ConfigError):
            validate(bad, "preflight")


def test_new_install_plan_is_stable_across_configure(raw, tmp_path):
    key = tmp_path / "ssh"
    key.write_text("key")
    key.chmod(0o600)
    raw.update(
        target={"mode": "new", "installation_id": "registry", "node_name": "worker"},
        cluster={"id": "cluster", "kubeconfig_ref": str(key), "context": "test"},
        nodes=[
            {
                "name": "worker",
                "uid": "node-uid",
                "ssh_host": "192.0.2.1",
                "ssh_user": "ubuntu",
                "private_key_ref": str(key),
                "known_hosts_ref": str(key),
            }
        ],
        install={
            "data_path": "/srv/harbor/data",
            "mount_path": "/srv/harbor",
            "capacity_gb": 160,
            "database_password_ref": raw["endpoint"]["admin_password_ref"],
        },
        tls={"mode": "self_signed"},
    )
    c = validate(raw, "preflight")
    raw["plan_digest"] = plan_digest(c)
    assert plan_digest(validate(raw, "configure")) == raw["plan_digest"]


def test_result_schema_matches_early_errors(raw, tmp_path):
    from pathlib import Path

    import jsonschema

    schema = json.loads(
        (Path(__file__).parents[2] / "docs/harbor-infra/result.schema.json").read_text()
    )
    raw["plan_digest"] = "0" * 64
    jsonschema.validate(run("configure", validate(raw, "configure")), schema)


@pytest.mark.parametrize("value", ["x" * 23, "A" * 24 + "$HOME", "A" * 24 + "\nNEXT=1"])
def test_bootstrap_secret_rejects_dotenv_interpretation(value):
    from migration.harbor_infra.config import bootstrap_secret

    with pytest.raises(ConfigError):
        bootstrap_secret(value, "password")
