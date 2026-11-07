import copy
import json
from pathlib import Path

import pytest
import yaml

from migration.openbao_infra import handoff
from migration.openbao_infra.config import ASSETS, ConfigError, secret_file, validate
from migration.openbao_infra.runner import aggregate, inventory

from .conftest import common, keycloak, material


@pytest.mark.parametrize(
    "operation",
    ["discover", "inspect", "preflight", "install", "configure", "verify", "handoff"],
)
def test_valid_input(raw_config, operation):
    assert validate(raw_config, operation)["request_id"] == "request-1"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(password="do-not-echo-this"),
        lambda c: c["target"].update(port=True),
        lambda c: c["target"].update(node_name="other"),
        lambda c: c["target"].update(installation_id='x"; injected'),
        lambda c: c["endpoint"].update(url="http://192.0.2.1:8200"),
        lambda c: c["endpoint"].update(url="https://user:secret@192.0.2.1:8200"),
        lambda c: c["endpoint"].update(
            url="https://192.0.2.1:8200/?secret=do-not-echo-this"
        ),
        lambda c: c["nodes"].append(copy.deepcopy(c["nodes"][0])),
        lambda c: c["handoff"].update(action="ack", download_confirmed=False),
    ],
)
def test_reject_unsafe_inputs_without_echo(raw_config, mutation):
    mutation(raw_config)
    with pytest.raises(ConfigError) as exc:
        validate(raw_config, "handoff")
    assert "do-not-echo-this" not in str(exc.value)


def test_private_credentials_reject_world_readable_and_symlink(tmp_path):
    source = tmp_path / "secret"
    source.write_text("unprinted-secret")
    source.chmod(0o644)
    with pytest.raises(ConfigError):
        secret_file(str(source))
    source.chmod(0o600)
    assert secret_file(str(source)) == "unprinted-secret"
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        secret_file(str(link))


def test_inventory_selects_exactly_one_node(raw_config):
    c = validate(raw_config, "install")
    more = copy.deepcopy(c["nodes"][0])
    more.update(name="node-2", uid="uid-2")
    c["nodes"].append(more)
    hosts = inventory(c, "install")["all"]["children"]["openbao_nodes"]["hosts"]
    assert len(hosts) == 1
    assert (
        "StrictHostKeyChecking=yes"
        in next(iter(hosts.values()))["ansible_ssh_common_args"]
    )


def bundle_file(raw_config, tmp_path):
    p = tmp_path / "bundle.json"
    handoff.private_json(
        p, {"installation_id": "test", "static_key_base64": "test-only"}
    )
    return p


def test_download_ack_deletes_only_temporary_copy(raw_config, tmp_path):
    c = validate(raw_config, "handoff")
    source = bundle_file(raw_config, tmp_path)
    prepared = handoff.prepare(c, source, now=100)
    artifact = Path(prepared["artifact_ref"])
    assert artifact.exists() and artifact.stat().st_mode & 0o077 == 0
    assert "test-only" not in json.dumps(prepared)
    c["handoff"].update(action="ack", download_confirmed=True)
    assert handoff.complete(c, now=110)["status"] == "handoff_acknowledged"
    assert not artifact.exists() and source.exists()
    assert handoff.complete(c, now=111)["status"] == "handoff_acknowledged"


def test_download_owner_target_binding_and_expiry(raw_config, tmp_path):
    c = validate(raw_config, "handoff")
    prepared = handoff.prepare(c, bundle_file(raw_config, tmp_path), now=100)
    c["handoff"].update(action="ack", download_confirmed=True, owner_id="wrong-user")
    with pytest.raises(ConfigError):
        handoff.complete(c, now=110)
    assert Path(prepared["artifact_ref"]).exists()
    c["handoff"].update(owner_id="user-1")
    with pytest.raises(ConfigError):
        handoff.complete(c, now=161)
    c["handoff"]["action"] = "cleanup"
    assert handoff.complete(c, now=161)["download_acknowledged"] is False
    assert not Path(prepared["artifact_ref"]).exists()


def test_download_reject_tampered_artifact(raw_config, tmp_path):
    c = validate(raw_config, "handoff")
    prepared = handoff.prepare(c, bundle_file(raw_config, tmp_path), now=100)
    Path(prepared["artifact_ref"]).write_text("changed")
    c["handoff"].update(action="ack", download_confirmed=True)
    with pytest.raises(ConfigError):
        handoff.complete(c, now=110)


def test_partial_discovery_cannot_report_absence_as_complete(raw_config):
    result = aggregate(
        "discover",
        validate(raw_config, "discover"),
        [
            {
                "host": "controller",
                "report": {
                    "status": "discovered",
                    "nodes": [{"name": "unknown-node"}],
                    "candidates": [],
                },
            }
        ],
        0,
    )
    assert result["status"] == "partially_ready"
    assert "HOST_DISCOVERY_INCOMPLETE" in result["reason_codes"]


def test_certificate_matches_address_and_private_key():
    cert, key = material.create_certificate(None, "192.0.2.1", 1)
    material.validate_certificate(cert, key, "192.0.2.1")
    with pytest.raises(common.BaoError, match="TLS_ADDRESS_MISMATCH"):
        material.validate_certificate(cert, key, "192.0.2.2")
    _, other = material.create_certificate(None, "192.0.2.1", 1)
    with pytest.raises(common.BaoError, match="TLS_KEY_MISMATCH"):
        material.validate_certificate(cert, other, "192.0.2.1")


def test_keycloak_client_does_not_enable_password_or_service_login():
    c = {
        "target": {"installation_id": "test"},
        "keycloak": {
            "client_id": "openbao-operators",
            "groups_claim": "groups",
            "redirect_uris": ["https://bao.invalid/callback"],
        },
    }
    result = keycloak.desired(c)
    assert result["standardFlowEnabled"]
    assert (
        not result["directAccessGrantsEnabled"] and not result["serviceAccountsEnabled"]
    )
    assert result["protocolMappers"][0]["config"]["full.path"] == "true"
    assert common.same({**result, "id": "server-generated"}, result)


def test_packaged_playbooks_exist_and_secret_tasks_do_not_debug():
    for p in (ASSETS / "playbooks").glob("*.yml"):
        assert isinstance(yaml.safe_load(p.read_text()), list)
    assert "debug:" not in "\n".join(p.read_text() for p in ASSETS.rglob("*.yml"))
    assert not list(ASSETS.rglob("*.retry"))


def test_http_redirects_are_never_followed():
    assert (
        common.NoRedirect().redirect_request(
            None, None, 307, "", {}, "https://other.invalid"
        )
        is None
    )


def test_reuse_cannot_restart_or_change_seal(raw_config, tmp_path):
    raw_config["target"]["mode"] = "reuse"
    raw_config["verification"] = {"restart": True}
    p = tmp_path / "token"
    p.write_text("token")
    raw_config["endpoint"].update(
        product_confirmed=True, ca_file=str(p), admin_token_ref=str(p)
    )
    with pytest.raises(ConfigError, match="reuse_non_disruptive"):
        validate(raw_config, "verify")
