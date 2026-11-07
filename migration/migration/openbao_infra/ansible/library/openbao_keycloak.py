#!/usr/bin/python
"""Own exactly one OpenBao OIDC client, never realms/users/other clients."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request

from ansible.module_utils.bao_common import BaoError, Client, ensure, same
from ansible.module_utils.basic import AnsibleModule


def desired(config):
    kc = config["keycloak"]
    return {
        "clientId": kc["client_id"],
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": kc["redirect_uris"],
        "fullScopeAllowed": False,
        "attributes": {
            "openbao-infra-owner": config["target"]["installation_id"],
            "pkce.code.challenge.method": "S256",
        },
        "protocolMappers": [
            {
                "name": "openbao-groups",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-group-membership-mapper",
                "consentRequired": False,
                "config": {
                    "claim.name": kc["groups_claim"],
                    "full.path": "true",
                    "id.token.claim": "true",
                    "access.token.claim": "false",
                    "userinfo.token.claim": "true",
                },
            }
        ],
    }


def configure(c):
    kc = c["keycloak"]
    ensure(kc["manage_client"], "KEYCLOAK_MANAGEMENT_NOT_SELECTED")
    realm = urlsplit(kc["issuer"]).path.split("/")[-1]
    ensure(re.fullmatch(r"[A-Za-z0-9_-]+", realm) is not None, "KEYCLOAK_REALM_INVALID")
    base = kc["admin_url"] + "/admin/realms/" + quote(realm, safe="") + "/clients"
    http = Client(kc["admin_url"], kc["ca_ref"])

    def call(method, url, body=None):
        req = Request(
            url,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + c["credentials"]["keycloak_admin_token"],
            },
        )
        try:
            with http.opener.open(req, timeout=20) as response:
                raw = response.read(1048577)
                ensure(len(raw) <= 1048576, "KEYCLOAK_RESPONSE_INVALID")
                return json.loads(raw) if raw else None
        except (HTTPError, URLError, OSError, ValueError):
            raise BaoError("KEYCLOAK_REQUEST_FAILED") from None

    found = call(
        "GET", base + "?clientId=" + quote(kc["client_id"], safe="") + "&search=false"
    )
    ensure(isinstance(found, list) and len(found) <= 1, "KEYCLOAK_CLIENT_AMBIGUOUS")
    wanted = desired(c)
    if not found:
        call("POST", base, {**wanted, "secret": c["credentials"]["oidc_client_secret"]})
        return True
    entry = found[0]
    ensure(
        entry.get("clientId") == kc["client_id"]
        and entry.get("attributes", {}).get("openbao-infra-owner")
        == c["target"]["installation_id"],
        "KEYCLOAK_CLIENT_OWNERSHIP_CONFLICT",
    )
    # Do not replace unrelated attributes/mappers or rotate the secret on repeat.
    ensure(same(entry, wanted), "KEYCLOAK_CLIENT_DRIFT")
    current_secret = call(
        "GET", base + "/" + quote(entry["id"], safe="") + "/client-secret"
    )
    ensure(
        current_secret.get("value") == c["credentials"]["oidc_client_secret"],
        "KEYCLOAK_CLIENT_SECRET_MISMATCH",
    )
    return False


def main():
    m = AnsibleModule(
        argument_spec={"config": {"type": "dict", "required": True, "no_log": True}}
    )
    try:
        m.params["config"]["credentials"] = m.params["credentials"]
        changed = configure(m.params["config"])
        m.exit_json(changed=changed, report={"status": "keycloak_client_ready"})
    except BaoError as exc:
        m.fail_json(msg="Keycloak client preparation blocked", reason_code=str(exc))
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        m.fail_json(
            msg="Keycloak client preparation failed",
            reason_code="KEYCLOAK_CLIENT_FAILED",
        )


if __name__ == "__main__":
    main()
