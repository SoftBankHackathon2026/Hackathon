import json

import pytest

from migration.harbor_infra.ansible.module_utils.harbor_core import (
    HarborError,
    configure,
    locked,
    read,
    save,
)
from migration.harbor_infra.config import validate
from migration.harbor_infra.handoff import execute, state_directory


def prepared(raw, api):
    c = validate(raw, "preflight")
    result = configure(
        api, c, {"oidc_client_secret": "secret-client"}, state_directory(c)
    )
    return c, result


def test_configure_and_repeat_preserve_credentials(raw, api):
    c, first = prepared(raw, api)
    path = state_directory(c) / "credentials.json"
    before = path.read_bytes()
    again = configure(
        api, c, {"oidc_client_secret": "secret-client"}, state_directory(c)
    )
    assert first["changed"] and not again["changed"] and path.read_bytes() == before
    assert len(api.robots) == 2
    assert api.robots[1]["permissions"][0]["access"] == [
        {"resource": "repository", "action": "pull", "effect": "allow"},
        {"resource": "repository", "action": "push", "effect": "allow"},
    ]
    assert api.robots[2]["permissions"][0]["access"] == [
        {"resource": "repository", "action": "pull", "effect": "allow"}
    ]
    assert "secret" not in json.dumps(first)


def test_robot_create_response_loss_recovers_same_secret(raw, api):
    c = validate(raw, "preflight")
    api.lose_robot_response = True
    with pytest.raises(HarborError, match="TLS_OR_CONNECTION_FAILED"):
        configure(api, c, {"oidc_client_secret": "secret-client"}, state_directory(c))
    original = api.robots[1]["secret"]
    result = configure(
        api, c, {"oidc_client_secret": "secret-client"}, state_directory(c)
    )
    assert result["status"] == "configured" and len(api.robots) == 2
    assert (
        read(state_directory(c) / "credentials.json")["accounts"]["demo/writer"][
            "secret"
        ]
        == original
    )


@pytest.mark.parametrize("metadata", [{"public": "true"}, {"public": "false"}])
def test_existing_project_needs_explicit_adoption(raw, api, metadata):
    api.projects["demo"] = {"project_id": 3, "metadata": metadata}
    c = validate(raw, "preflight")
    with pytest.raises(HarborError, match="EXISTING_PROJECT_NOT_ADOPTED"):
        configure(api, c, {"oidc_client_secret": "secret"}, state_directory(c))
    assert not api.robots


@pytest.mark.parametrize(
    "change", ["public", "proxy", "privilege", "expired", "disabled", "missing_secret"]
)
def test_existing_resource_drift_is_not_overwritten(raw, api, change):
    c, _ = prepared(raw, api)
    if change == "public":
        api.projects["demo"]["metadata"]["public"] = "true"
    if change == "proxy":
        api.projects["demo"]["registry_id"] = 7
    if change == "privilege":
        api.robots[2]["permissions"][0]["access"].append(
            {"resource": "repository", "action": "delete"}
        )
    if change == "expired":
        api.robots[1]["expires_at"] = 1
    if change == "disabled":
        api.robots[1]["disable"] = True
    if change == "missing_secret":
        path = state_directory(c) / "credentials.json"
        state = read(path)
        del state["accounts"]["demo/writer"]
        save(path, state)
    before = len([v for v in api.calls if v[0] != "GET"])
    with pytest.raises(HarborError):
        configure(api, c, {"oidc_client_secret": "secret-client"}, state_directory(c))
    assert len([v for v in api.calls if v[0] != "GET"]) == before


def test_local_users_block_oidc_switch(raw, api):
    api.users.append({"username": "existing-person"})
    with pytest.raises(HarborError, match="OIDC_LOCAL_USERS_EXIST"):
        prepared(raw, api)
    assert not any(method == "PUT" for method, _, _ in api.calls)


def test_group_role_mapping_is_explicit(raw, api):
    raw["projects"][0]["groups"] = [{"name": "/operators", "role_id": 2}]
    c, _ = prepared(raw, api)
    configure(api, c, {"oidc_client_secret": "secret-client"}, state_directory(c))
    assert len(api.groups) == len(api.members["demo"]) == 1
    assert api.members["demo"][0]["role_id"] == 2


def test_handoff_is_bound_and_cleanup_preserves_operational_secret(raw, api):
    c, _ = prepared(raw, api)
    c["handoff"]["action"] = "prepare"
    result = execute(c)
    export = state_directory(c) / "export.json"
    assert export.exists() and export.stat().st_mode & 0o777 == 0o600
    c["handoff"].update(action="ack", receipt_digest="0" * 64)
    with pytest.raises(HarborError):
        execute(c)
    assert export.exists()
    c["handoff"]["receipt_digest"] = result["sha256"]
    assert execute(c)["status"] == "handoff_acknowledged"
    assert not export.exists() and (state_directory(c) / "credentials.json").exists()
    c["handoff"]["action"] = "cleanup"
    assert execute(c)["status"] == "handoff_cleaned"


def test_handoff_target_mismatch_rejected(raw, api):
    c, _ = prepared(raw, api)
    path = state_directory(c) / "credentials.json"
    state = read(path)
    state["binding"]["endpoint"] = "https://other.example"
    save(path, state)
    c["handoff"]["action"] = "prepare"
    with pytest.raises(HarborError, match="CREDENTIALS_NOT_READY"):
        execute(c)


def test_handoff_cannot_clean_unacknowledged_live_export(raw, api):
    c, _ = prepared(raw, api)
    c["handoff"]["action"] = "prepare"
    execute(c)
    c["handoff"]["action"] = "cleanup"
    with pytest.raises(HarborError):
        execute(c)
    record = state_directory(c) / "export-record.json"
    state = read(record)
    state["expires_at"] = 1
    save(record, state)
    execute(c)
    assert not (state_directory(c) / "export.json").exists()


def test_target_lock_is_exclusive(tmp_path):
    with locked(tmp_path / "state"):
        with pytest.raises(HarborError, match="TARGET_LOCKED"):
            with locked(tmp_path / "state"):
                pass


def test_cleanup_still_works_without_operational_credentials(raw, api):
    c, _ = prepared(raw, api)
    c["handoff"]["action"] = "prepare"
    execute(c)
    root = state_directory(c)
    state = read(root / "export-record.json")
    state["expires_at"] = 1
    save(root / "export-record.json", state)
    (root / "credentials.json").unlink()
    c["handoff"]["action"] = "cleanup"
    assert execute(c)["status"] == "handoff_cleaned"
    assert not (root / "export.json").exists()


def test_oidc_secret_change_is_never_ignored_or_rotated(raw, api):
    c, _ = prepared(raw, api)
    with pytest.raises(
        HarborError, match="OIDC_SECRET_CHANGE_REQUIRES_SEPARATE_OPERATION"
    ):
        configure(
            api, c, {"oidc_client_secret": "different-secret"}, state_directory(c)
        )
