"""Bounded, redirect-free Harbor REST and private atomic state primitives."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class HarborError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def check(ok, code):
    if not ok:
        raise HarborError(code)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def private_dir(path):
    p = Path(path)
    # Refuse symlinks in every existing parent, not just the leaf.
    for part in (p, *p.parents):
        check(not part.is_symlink(), "STATE_SYMLINK_REJECTED")
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = p.stat()
    check(
        st.st_uid == os.getuid() and not st.st_mode & 0o077, "STATE_PERMISSIONS_INVALID"
    )
    return p


def atomic(path, data, mode=0o600):
    p = Path(path)
    check(not p.is_symlink(), "STATE_SYMLINK_REJECTED")
    fd, name = tempfile.mkstemp(dir=p.parent, prefix=".harbor-")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, p)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(path, value):
    atomic(path, json.dumps(value, sort_keys=True))


def read(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        check(
            stat.S_ISREG(st.st_mode)
            and not st.st_mode & 0o077
            and st.st_uid == os.getuid(),
            "STATE_PERMISSIONS_INVALID",
        )
        return json.load(f)


@contextlib.contextmanager
def locked(directory):
    p = private_dir(directory)
    fd = os.open(p / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HarborError("TARGET_LOCKED") from None
        yield p
    finally:
        os.close(fd)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise HarborError("HTTP_REDIRECT_REJECTED")


class Api:
    def __init__(self, endpoint, ca="", username="", password=""):
        parsed = urllib.parse.urlsplit(endpoint)
        check(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment,
            "ENDPOINT_INVALID",
        )
        self.endpoint = endpoint.rstrip("/")
        ctx = ssl.create_default_context(cadata=ca or None)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self.authorization = (
            "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
            if username
            else ""
        )

    def call(self, method, path, body=None, expected=(200,), authenticated=True):
        check(
            path.startswith("/") and not path.startswith("//") and "\\" not in path,
            "API_PATH_REJECTED",
        )
        headers = {"Accept": "application/json"}
        if path.startswith("/api/v2.0/projects/"):
            headers["X-Is-Resource-Name"] = "true"
        if self.authorization and authenticated:
            headers["Authorization"] = self.authorization
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.endpoint + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(req, timeout=20) as response:
                status, payload = response.status, response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, b""
            exc.close()
        except HarborError:
            raise
        except Exception:
            raise HarborError("TLS_OR_CONNECTION_FAILED") from None
        check(status in expected, "API_HTTP_" + str(status))
        check(len(payload) <= 4 * 1024 * 1024, "API_RESPONSE_TOO_LARGE")
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            raise HarborError("API_RESPONSE_INVALID") from None

    def pages(self, path):
        result = []
        for page in range(1, 101):
            sep = "&" if "?" in path else "?"
            values = self.call("GET", path + sep + "page_size=100&page=" + str(page))
            check(isinstance(values, list), "API_RESPONSE_INVALID")
            result.extend(values)
            if len(values) < 100:
                return result
        raise HarborError("API_PAGINATION_LIMIT")


def inspect(api):
    health = api.call("GET", "/api/v2.0/health", authenticated=False)
    check(
        isinstance(health, dict) and isinstance(health.get("components"), list),
        "PRODUCT_UNCONFIRMED",
    )
    info = api.call("GET", "/api/v2.0/systeminfo", expected=(200, 401, 403)) or {}
    version = info.get("harbor_version")
    confirmed = isinstance(version, str) and version.startswith("v2.")
    return {
        "status": "inspected"
        if confirmed and health.get("status") == "healthy"
        else "partially_ready",
        "product_confirmed": confirmed,
        "version": version if confirmed else None,
        "healthy": health.get("status") == "healthy",
        "reason_codes": [] if confirmed else ["PRODUCT_UNCONFIRMED"],
    }


def config_value(configuration, key):
    v = configuration.get(key)
    return v.get("value") if isinstance(v, dict) else v


def permissions(project, write=False):
    actions = ["pull", "push"] if write else ["pull"]
    return [
        {
            "kind": "project",
            "namespace": project,
            "access": [
                {"resource": "repository", "action": a, "effect": "allow"}
                for a in actions
            ],
        }
    ]


def normalized_permissions(values):
    return sorted(
        (
            v.get("kind"),
            v.get("namespace"),
            a.get("resource"),
            a.get("action"),
            a.get("effect", "allow"),
        )
        for v in values
        for a in v.get("access", [])
    )


def oidc_settings(kc):
    return {
        "auth_mode": "oidc_auth",
        "oidc_name": "Keycloak",
        "oidc_endpoint": kc["issuer_url"],
        "oidc_client_id": kc["client_id"],
        "oidc_groups_claim": kc["groups_claim"],
        "oidc_user_claim": kc["username_claim"],
        "oidc_scope": kc["scope"],
        "oidc_verify_cert": True,
        "oidc_auto_onboard": True,
        "oidc_admin_group": kc["admin_group"],
    }


def configure(api, c, secrets_value, directory):
    """Add only declared resources. Persist robot secrets BEFORE the create request."""
    owner = (
        "oneport-harbor:" + c["organization_id"] + ":" + c["target"]["installation_id"]
    )
    binding = {
        "organization_id": c["organization_id"],
        "installation_id": c["target"]["installation_id"],
        "endpoint": c["endpoint"]["url"],
    }
    with locked(directory) as root:
        state_path = root / "managed.json"
        state = read(
            state_path, {"binding": binding, "projects": {}, "robots": {}, "groups": {}}
        )
        check(state["binding"] == binding, "STATE_TARGET_MISMATCH")
        cfg = api.call("GET", "/api/v2.0/configurations")
        kc = c["keycloak"]
        wanted = oidc_settings(kc)
        mode = config_value(cfg, "auth_mode")
        check(mode in ("db_auth", "oidc_auth"), "EXISTING_AUTH_MODE_CONFLICT")
        if mode == "db_auth":
            users = api.pages("/api/v2.0/users")
            check(
                all(u.get("username") == "admin" for u in users),
                "OIDC_LOCAL_USERS_EXIST",
            )
        else:
            check(
                config_value(cfg, "oidc_endpoint") == kc["issuer_url"]
                and config_value(cfg, "oidc_client_id") == kc["client_id"],
                "OIDC_PROVIDER_CONFLICT",
            )
        secret_digest = hashlib.sha256(
            secrets_value["oidc_client_secret"].encode()
        ).hexdigest()
        if state.get("oidc_secret_digest"):
            check(
                state["oidc_secret_digest"] == secret_digest,
                "OIDC_SECRET_CHANGE_REQUIRES_SEPARATE_OPERATION",
            )
        if state.get("oidc_digest"):
            check(
                state["oidc_digest"] == digest(wanted), "MANAGED_OIDC_SETTINGS_CHANGED"
            )
        changed = False
        if any(config_value(cfg, k) != v for k, v in wanted.items()):
            # Existing OIDC provider settings may only be adopted as-is.
            check(mode == "db_auth", "EXISTING_OIDC_SETTINGS_CONFLICT")
            api.call(
                "PUT",
                "/api/v2.0/configurations",
                {**wanted, "oidc_client_secret": secrets_value["oidc_client_secret"]},
            )
            changed = True
        state["oidc_digest"] = digest(wanted)
        state["oidc_secret_digest"] = secret_digest
        save(state_path, state)
        credentials_path = root / "credentials.json"
        credentials = read(credentials_path, {"binding": binding, "accounts": {}})
        check(credentials["binding"] == binding, "STATE_TARGET_MISMATCH")
        reports = []
        for project in c["projects"]:
            name = project["name"]
            qname = urllib.parse.quote(name, safe="")
            remote = api.call("GET", "/api/v2.0/projects/" + qname, expected=(200, 404))
            if remote is None:
                check(name not in state["projects"], "MANAGED_PROJECT_MISSING")
                api.call(
                    "POST",
                    "/api/v2.0/projects",
                    {"project_name": name, "metadata": {"public": "false"}},
                    expected=(201,),
                )
                remote = api.call("GET", "/api/v2.0/projects/" + qname)
                state["projects"][name] = remote["project_id"]
                save(state_path, state)
                changed = True
            else:
                check(
                    state["projects"].get(name) == remote["project_id"]
                    or project["adopt_existing"],
                    "EXISTING_PROJECT_NOT_ADOPTED",
                )
                state["projects"][name] = remote["project_id"]
                save(state_path, state)
            check(
                remote.get("metadata", {}).get("public") == "false"
                and not remote.get("registry_id"),
                "PROJECT_NOT_PRIVATE_OR_IS_PROXY",
            )
            for group in project["groups"]:
                matches = [
                    g
                    for g in api.pages("/api/v2.0/usergroups")
                    if g.get("group_name") == group["name"]
                ]
                check(
                    not matches
                    or len(matches) == 1
                    and matches[0].get("group_type") == 3,
                    "GROUP_TYPE_CONFLICT",
                )
                if not matches:
                    api.call(
                        "POST",
                        "/api/v2.0/usergroups",
                        {"group_name": group["name"], "group_type": 3},
                        expected=(201,),
                    )
                    matches = [
                        g
                        for g in api.pages("/api/v2.0/usergroups")
                        if g.get("group_name") == group["name"]
                        and g.get("group_type") == 3
                    ]
                    changed = True
                check(len(matches) == 1, "GROUP_LOOKUP_FAILED")
                members = api.pages("/api/v2.0/projects/" + qname + "/members")
                members = [
                    m
                    for m in members
                    if m.get("entity_type") == "g"
                    and m.get("entity_id") == matches[0]["id"]
                ]
                if members:
                    check(
                        len(members) == 1
                        and members[0].get("role_id") == group["role_id"],
                        "GROUP_ROLE_CONFLICT",
                    )
                else:
                    api.call(
                        "POST",
                        "/api/v2.0/projects/" + qname + "/members",
                        {
                            "role_id": group["role_id"],
                            "member_group": {"id": matches[0]["id"]},
                        },
                        expected=(201,),
                    )
                    changed = True
            for role, days in [
                ("writer", project["writer_days"]),
                ("reader", project["reader_days"]),
            ]:
                key = name + "/" + role
                suffix = "hi-" + c["target"]["installation_id"] + "-" + role
                marker = owner + ":" + key
                wanted_permissions = permissions(name, role == "writer")
                robots = [
                    r
                    for r in api.pages("/api/v2.0/robots")
                    if r.get("description") == marker
                ]
                check(len(robots) <= 1, "ROBOT_DUPLICATE")
                if not robots:
                    check(key not in state["robots"], "MANAGED_ROBOT_MISSING")
                    if key not in credentials["accounts"]:
                        credentials["accounts"][key] = {
                            "secret": secrets.token_urlsafe(36),
                            "pending": True,
                        }
                        save(credentials_path, credentials)
                    staged = credentials["accounts"][key]
                    created = api.call(
                        "POST",
                        "/api/v2.0/robots",
                        {
                            "name": suffix,
                            "description": marker,
                            "level": "project",
                            "duration": days,
                            "disable": False,
                            "permissions": wanted_permissions,
                            "secret": staged["secret"],
                        },
                        expected=(201,),
                    )
                    robots = [api.call("GET", "/api/v2.0/robots/" + str(created["id"]))]
                    changed = True
                robot = robots[0]
                check(
                    robot.get("level") == "project"
                    and not robot.get("disable")
                    and normalized_permissions(robot.get("permissions", []))
                    == normalized_permissions(wanted_permissions)
                    and robot.get("duration") == days
                    and robot.get("expires_at", 0) > time.time(),
                    "ROBOT_POLICY_CONFLICT_OR_EXPIRED",
                )
                check(key in credentials["accounts"], "ROBOT_SECRET_UNAVAILABLE")
                check(
                    state["robots"].get(key, robot["id"]) == robot["id"],
                    "ROBOT_IDENTITY_CHANGED",
                )
                credentials["accounts"][key].update(
                    {
                        "id": robot["id"],
                        "username": robot["name"],
                        "expires_at": robot["expires_at"],
                        "pending": False,
                        "project": name,
                        "role": role,
                    }
                )
                state["robots"][key] = robot["id"]
                save(credentials_path, credentials)
                save(state_path, state)
                reports.append(
                    {
                        k: credentials["accounts"][key][k]
                        for k in ("id", "username", "expires_at", "project", "role")
                    }
                )
        state["configured"] = True
        save(state_path, state)
        return {
            "status": "configured",
            "changed": changed,
            "accounts": reports,
            "credential_ref": str(credentials_path),
            "reason_codes": [],
            "oidc_browser_login_verified": False,
        }


def check_oidc(kc, ca=""):
    issuer = kc["issuer_url"].rstrip("/")
    context = ssl.create_default_context(cadata=ca or None)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect(),
        urllib.request.HTTPSHandler(context=context),
    )
    try:
        with opener.open(
            issuer + "/.well-known/openid-configuration", timeout=20
        ) as response:
            data = response.read(1024 * 1024 + 1)
        check(len(data) <= 1024 * 1024, "OIDC_METADATA_TOO_LARGE")
        metadata = json.loads(data)
        check(metadata.get("issuer") == issuer, "OIDC_ISSUER_MISMATCH")
        authority = urllib.parse.urlsplit(issuer).netloc
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            u = urllib.parse.urlsplit(metadata.get(key, ""))
            check(
                u.scheme == "https"
                and u.netloc == authority
                and not u.username
                and not u.password,
                "OIDC_ENDPOINT_INVALID",
            )
    except HarborError:
        raise
    except Exception:
        raise HarborError("OIDC_METADATA_UNREACHABLE_OR_INVALID") from None


def ensure_keycloak_client(c, values):
    """Optionally own one confidential client in an existing realm; never replace."""
    kc = c["keycloak"]
    if not kc["manage_client"]:
        return False
    import re

    issuer = urllib.parse.urlsplit(kc["issuer_url"])
    match = re.fullmatch(r"/realms/([A-Za-z0-9_-]{1,64})/?", issuer.path)
    check(match is not None, "KEYCLOAK_ADMIN_PATH_UNSUPPORTED")
    api = Api(issuer.scheme + "://" + issuer.netloc, values.get("oidc_ca", ""))
    api.authorization = "Bearer " + values["keycloak_admin_token"]
    path = "/admin/realms/" + urllib.parse.quote(match.group(1), safe="") + "/clients"
    query = (
        path
        + "?"
        + urllib.parse.urlencode({"clientId": kc["client_id"], "search": "false"})
    )
    found = api.call("GET", query)
    check(isinstance(found, list) and len(found) <= 1, "KEYCLOAK_CLIENT_AMBIGUOUS")
    owner = c["organization_id"] + ":" + c["target"]["installation_id"]
    wanted = {
        "clientId": kc["client_id"],
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "standardFlowEnabled": True,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "fullScopeAllowed": False,
        "redirectUris": [c["endpoint"]["url"] + "/c/oidc/callback"],
        "attributes": {"oneport-harbor-owner": owner},
        "protocolMappers": [
            {
                "name": "oneport-harbor-groups",
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
    if not found:
        api.call(
            "POST",
            path,
            {**wanted, "secret": values["oidc_client_secret"]},
            expected=(201,),
        )
        return True
    existing = found[0]
    check(
        existing.get("clientId") == kc["client_id"]
        and existing.get("attributes", {}).get("oneport-harbor-owner") == owner,
        "KEYCLOAK_CLIENT_OWNERSHIP_CONFLICT",
    )
    for key, expected in wanted.items():
        if key in ("attributes", "protocolMappers"):
            continue
        check(existing.get(key) == expected, "KEYCLOAK_CLIENT_SETTINGS_CONFLICT")
    mappers = [
        m
        for m in existing.get("protocolMappers", [])
        if m.get("name") == "oneport-harbor-groups"
    ]
    check(
        len(mappers) == 1
        and all(
            mappers[0].get(k) == v for k, v in wanted["protocolMappers"][0].items()
        ),
        "KEYCLOAK_GROUP_MAPPER_CONFLICT",
    )
    secret = api.call(
        "GET",
        path + "/" + urllib.parse.quote(existing["id"], safe="") + "/client-secret",
    )
    check(
        secrets.compare_digest(secret.get("value", ""), values["oidc_client_secret"]),
        "KEYCLOAK_CLIENT_SECRET_CONFLICT",
    )
    return False
