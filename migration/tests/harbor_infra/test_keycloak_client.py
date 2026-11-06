import copy

import pytest
from migration.harbor_infra.ansible.module_utils import harbor_core as core
from migration.harbor_infra.config import validate


def test_client_management_is_opt_in(raw):
    c = validate(raw, "preflight")
    assert core.ensure_keycloak_client(c, {}) is False


def test_owned_client_creation_repeat_and_drift(raw, monkeypatch):
    c = validate(raw, "preflight")
    c["keycloak"]["manage_client"] = True
    clients = []

    class Client:
        def __init__(self, *args):
            pass

        def call(self, method, path, body=None, **kwargs):
            if method == "POST":
                clients.append({**copy.deepcopy(body), "id": "client-1"})
                return None
            if path.endswith("/client-secret"):
                return {"value": "provided-secret"}
            return copy.deepcopy(clients)

    monkeypatch.setattr(core, "Api", Client)
    values = {
        "keycloak_admin_token": "admin-secret",
        "oidc_client_secret": "provided-secret",
    }
    assert core.ensure_keycloak_client(c, values) is True
    assert clients[0]["redirectUris"] == [c["endpoint"]["url"] + "/c/oidc/callback"]
    assert not clients[0]["publicClient"] and not clients[0]["serviceAccountsEnabled"]
    assert core.ensure_keycloak_client(c, values) is False
    clients[0]["redirectUris"] = ["https://other.example/callback"]
    with pytest.raises(core.HarborError, match="KEYCLOAK_CLIENT_SETTINGS_CONFLICT"):
        core.ensure_keycloak_client(c, values)


def test_unowned_client_is_not_changed(raw, monkeypatch):
    c = validate(raw, "preflight")
    c["keycloak"]["manage_client"] = True

    class Client:
        def __init__(self, *args):
            pass

        def call(self, *args, **kwargs):
            return [
                {
                    "clientId": c["keycloak"]["client_id"],
                    "id": "other",
                    "attributes": {},
                }
            ]

    monkeypatch.setattr(core, "Api", Client)
    with pytest.raises(core.HarborError, match="KEYCLOAK_CLIENT_OWNERSHIP_CONFLICT"):
        core.ensure_keycloak_client(c, {"keycloak_admin_token": "admin-secret"})
