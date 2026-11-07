from __future__ import annotations

from ..identity.adapters import Http, segment
from ..identity.common import IdentityError, digest, now, require
from .common import secret_file

PAGE = 100
PAGE_LIMIT = 100000

READ_ERRORS = (IdentityError, LookupError, TypeError, ValueError, AttributeError)

LIMITATIONS = [
    "CREDENTIALS_PASSWORDS_AND_MFA_NOT_EXPORTED",
    "COMPOSITE_ROLES_NOT_EXPANDED",
    "USER_DIRECT_ROLE_MAPPINGS_NOT_MIGRATED",
    "KEYCLOAK_ONLY_FEATURES_NOT_MIGRATED",
]


class KeycloakHttp(Http):
    def __init__(self, base_url, ca_file=None):
        super().__init__(base_url, ca_file=ca_file, token_value="")

    def form(self, path, data):
        import requests

        try:
            response = self.session.request(
                "POST",
                self.base + path,
                data=data,
                verify=self.verify,
                timeout=(10, 30),
                allow_redirects=False,
            )
        except requests.RequestException:
            raise IdentityError("SOURCE_TRANSPORT_ERROR", status="failed") from None
        if response.status_code != 200:
            raise IdentityError(
                "KEYCLOAK_TOKEN_HTTP_" + str(response.status_code), status="failed"
            )
        try:
            return response.json()
        except ValueError:
            raise IdentityError(
                "KEYCLOAK_TOKEN_RESPONSE_SHAPE", status="failed"
            ) from None


class Keycloak:
    def __init__(self, source, http=None):
        self.source = source
        self.http = http or KeycloakHttp(source["base_url"], source.get("ca_file"))
        self.admin = "/admin/realms/" + segment(source["realm"])
        self.login()

    def login(self):
        response = self.http.form(
            "/realms/"
            + segment(self.source["realm"])
            + "/protocol/openid-connect/token",
            {
                "grant_type": "client_credentials",
                "client_id": self.source["client_id"],
                "client_secret": secret_file(self.source["client_secret_file"]),
            },
        )
        token = response.get("access_token") if isinstance(response, dict) else None
        require(isinstance(token, str) and bool(token), "KEYCLOAK_TOKEN_MISSING")
        self.http.headers = {"Authorization": "Bearer " + token}

    def get(self, path, params=None):
        try:
            return self.http.request("GET", self.admin + path, params=params)
        except IdentityError as error:
            if error.code != "TARGET_HTTP_401":
                raise
        self.login()
        return self.http.request("GET", self.admin + path, params=params)

    def pages(self, path, params=None):
        items = []
        for first in range(0, PAGE_LIMIT, PAGE):
            page = self.get(path, {**(params or {}), "first": first, "max": PAGE})
            require(isinstance(page, list), "KEYCLOAK_RESPONSE_SHAPE")
            items.extend(page)
            if len(page) < PAGE:
                return items
        raise IdentityError("KEYCLOAK_PAGINATION_LIMIT")


def text(value, field):
    v = value.get(field) if isinstance(value, dict) else None
    require(isinstance(v, str) and bool(v), "KEYCLOAK_RESPONSE_SHAPE")
    return v


def optional_text(value):
    return value if isinstance(value, str) and value else None


def role_mappings(raw, realm):
    require(isinstance(raw, dict), "KEYCLOAK_RESPONSE_SHAPE")
    default = "default-roles-" + realm.lower()
    result = []
    for role in raw.get("realmMappings") or []:
        name = text(role, "name")
        if name != default:
            result.append({"kind": "realm_role", "role": name})
    clients = raw.get("clientMappings") or {}
    require(isinstance(clients, dict), "KEYCLOAK_RESPONSE_SHAPE")
    for key, value in sorted(clients.items()):
        require(isinstance(value, dict), "KEYCLOAK_RESPONSE_SHAPE")
        client = optional_text(value.get("client")) or key
        for role in value.get("mappings") or []:
            result.append(
                {"kind": "client_role", "client": client, "role": text(role, "name")}
            )
    return result


def collect(config, http=None):
    source = config["source"]
    kc = Keycloak(source, http)
    realm = source["realm"]
    inv = {
        "schema_version": 1,
        "kind": "identity_export_inventory",
        "collected_at": now(),
        "source": {k: source[k] for k in ("provider", "base_url", "realm")},
        "users": [],
        "groups": [],
        "memberships": [],
        "group_roles": [],
        "user_roles": [],
        "coverage": [],
        "limitations": list(LIMITATIONS),
    }

    def capture(scope, fn):
        try:
            result = fn()
            inv["coverage"].append({"scope": scope, "status": "complete"})
            return result
        except READ_ERRORS:
            inv["coverage"].append(
                {"scope": scope, "status": "unknown", "reason": "SOURCE_READ_FAILED"}
            )
            return None

    users = capture(
        "users", lambda: kc.pages("/users", {"briefRepresentation": "false"})
    )
    for u in users or []:
        username = text(u, "username")
        inv["users"].append(
            {
                "id": text(u, "id"),
                "username": username,
                "email": optional_text(u.get("email")),
                "first_name": optional_text(u.get("firstName")),
                "last_name": optional_text(u.get("lastName")),
                "enabled": u.get("enabled") is True,
                "email_verified": u.get("emailVerified") is True,
                "federated": bool(u.get("federationLink")),
                "service_account": username.startswith("service-account-")
                or bool(u.get("serviceAccountClientId")),
            }
        )
    for u in inv["users"]:
        mapped = capture(
            "user_roles:" + u["id"],
            lambda u=u: role_mappings(
                kc.get("/users/" + segment(u["id"]) + "/role-mappings"), realm
            ),
        )
        inv["user_roles"].extend({"user_id": u["id"], **m} for m in mapped or [])

    def tree():
        found, seen = [], set()

        def walk(group, parent):
            gid, name = text(group, "id"), text(group, "name")
            require(gid not in seen, "KEYCLOAK_GROUP_CYCLE")
            seen.add(gid)
            path = optional_text(group.get("path")) or (
                (parent["path"] if parent else "") + "/" + name
            )
            node = {
                "id": gid,
                "name": name,
                "path": path,
                "parent_id": parent["id"] if parent else None,
            }
            found.append(node)
            children = group.get("subGroups") or []
            require(isinstance(children, list), "KEYCLOAK_RESPONSE_SHAPE")
            count = group.get("subGroupCount")
            if isinstance(count, int) and count > len(children):
                children = kc.pages(
                    "/groups/" + segment(gid) + "/children",
                    {"briefRepresentation": "false"},
                )
            for child in children:
                walk(child, node)

        for group in kc.pages("/groups", {"briefRepresentation": "false"}):
            walk(group, None)
        return found

    inv["groups"] = capture("groups", tree) or []
    for g in inv["groups"]:
        members = capture(
            "members:" + g["id"],
            lambda g=g: kc.pages(
                "/groups/" + segment(g["id"]) + "/members",
                {"briefRepresentation": "true"},
            ),
        )
        inv["memberships"].extend(
            {"group_id": g["id"], "user_id": text(m, "id")} for m in members or []
        )
        mapped = capture(
            "group_roles:" + g["id"],
            lambda g=g: role_mappings(
                kc.get("/groups/" + segment(g["id"]) + "/role-mappings"), realm
            ),
        )
        inv["group_roles"].extend({"group_id": g["id"], **m} for m in mapped or [])
    inv["status"] = (
        "partial"
        if any(c["status"] == "unknown" for c in inv["coverage"])
        else "complete"
    )
    inv["inventory_hash"] = digest(inv)
    return inv


def validate_inventory(inv):
    require(
        isinstance(inv, dict)
        and inv.get("kind") == "identity_export_inventory"
        and inv.get("schema_version") == 1,
        "INVALID_INVENTORY",
    )
    raw = {k: v for k, v in inv.items() if k != "inventory_hash"}
    require(inv.get("inventory_hash") == digest(raw), "INVENTORY_HASH_MISMATCH")
    for field in (
        "users",
        "groups",
        "memberships",
        "group_roles",
        "user_roles",
        "coverage",
    ):
        require(isinstance(inv.get(field), list), "INVALID_INVENTORY")
    for field, key in (
        ("users", "id"),
        ("users", "username"),
        ("groups", "id"),
        ("groups", "path"),
    ):
        values = [v[key] for v in inv[field]]
        require(len(values) == len(set(values)), "DUPLICATE_SOURCE_ID")
    users = {u["id"] for u in inv["users"]}
    groups = {g["id"] for g in inv["groups"]}
    require(
        all(m["group_id"] in groups for m in inv["memberships"])
        and all(r["group_id"] in groups for r in inv["group_roles"])
        and all(r["user_id"] in users for r in inv["user_roles"]),
        "ORPHAN_SOURCE_REFERENCE",
    )
    return inv
