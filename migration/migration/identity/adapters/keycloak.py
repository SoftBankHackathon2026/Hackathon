"""Keycloak 26 Admin REST collection and conservative new-group creation.

Only safe representation fields are retained, never client secrets or user credentials.
Directory synchronization, user provisioning and protocol mapper changes are unsupported.
"""
from __future__ import annotations

import os
from urllib.parse import quote, urlparse

from .base import check_operation, empty_inventory, observation, observation_key, same_observation
from ..models import (BlockedError, ConfigError, ConflictError, Endpoint, Operation,
                      UnknownOutcome, UnsupportedError, fields, reject_secrets)

MAX_PAGES = 1000
MAX_GROUPS = 100000

class _Denied(BlockedError):
    pass


class KeycloakAdapter:
    def __init__(self, endpoint: Endpoint, client=None):
        parsed = urlparse(endpoint.options.get("url", ""))
        if endpoint.provider != "keycloak" or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError("Keycloak requires an explicit HTTPS base URL")
        if "/" in endpoint.scope or endpoint.scope in {".", ".."}:
            raise ConfigError("invalid Keycloak realm")
        self.endpoint, self.client = endpoint, client
        self.base_url = endpoint.options["url"].rstrip("/")
        self.realm_path = f"/admin/realms/{quote(endpoint.scope, safe='')}"

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self.client.request(method, self.base_url + path, follow_redirects=False, **kwargs)
        except Exception:
            if method != "GET":
                raise UnknownOutcome("Keycloak write outcome unknown; re-observe before resume") from None
            raise BlockedError("Keycloak lookup failed") from None
        status = response.status_code
        if status in (401, 403):
            raise _Denied("Keycloak scoped lookup or write denied")
        if status == 404:
            return None
        if status == 409:
            raise ConflictError("Keycloak object already exists or changed")
        if not 200 <= status < 300:
            if method != "GET" and status >= 500:
                raise UnknownOutcome("Keycloak write outcome unknown")
            raise BlockedError("Keycloak request rejected")
        if method != "GET":
            return True
        try:
            return response.json()
        except Exception:
            raise BlockedError("Keycloak response is incomplete") from None

    def _pages(self, path: str, *, params: dict | None = None) -> list[dict]:
        result, seen, offset = [], set(), 0
        for _ in range(MAX_PAGES):
            page = self._request("GET", path, params={**(params or {}), "first": offset, "max": 100})
            if not isinstance(page, list):
                raise BlockedError("Keycloak list response is incomplete")
            if len(page) > 100:
                raise BlockedError("Keycloak response exceeds the requested page size")
            if any(not isinstance(item, dict) or not item.get("id") for item in page):
                raise BlockedError("Keycloak list item lacks immutable identifier")
            ids = [item["id"] for item in page]
            if len(ids) != len(set(ids)) or seen.intersection(ids):
                raise BlockedError("Keycloak paging is unstable or unsupported")
            seen.update(ids)
            result.extend(page)
            if len(page) < 100:
                return result
            offset += len(page)
        raise BlockedError("Keycloak pagination exceeded the supported page limit")

    def _groups(self) -> list[dict]:
        result, pending = [], self._pages(self.realm_path + "/groups", params={"briefRepresentation": "false", "populateHierarchy": "false"})
        seen = set()
        while pending:
            if len(seen) >= MAX_GROUPS:
                raise BlockedError("Keycloak group hierarchy exceeded the supported size limit")
            raw = pending.pop(0)
            if raw["id"] in seen:
                raise BlockedError("Keycloak group hierarchy repeats an identifier")
            seen.add(raw["id"])
            detail = self._request("GET", self.realm_path + "/groups/" + quote(raw["id"], safe=""))
            if not isinstance(detail, dict) or detail.get("id") != raw["id"]:
                raise BlockedError("Keycloak group changed during collection")
            clean = {key: detail[key] for key in ("id", "name", "path", "parentId", "attributes", "realmRoles", "clientRoles") if key in detail}
            reject_secrets(clean)
            result.append(clean)
            children = self._pages(self.realm_path + "/groups/" + quote(raw["id"], safe="") + "/children", params={"briefRepresentation": "false"})
            pending.extend(children)
        return result

    def _check(self, op: Operation) -> None:
        check_operation(self.endpoint, op, {"keycloak.group"})
        if op.resource != self.endpoint.resource or not op.resource.startswith("/") or len(op.resource.split("/")) != 2:
            raise UnsupportedError("only an explicitly allowed top-level Keycloak group is supported")
        fields(op.desired, {"name", "attributes"})
        if not isinstance(op.desired["name"], str) or not op.desired["name"] or "/" in op.desired["name"] or op.resource != "/" + op.desired["name"]:
            raise ConfigError("Keycloak group name does not match resource")
        attrs = op.desired["attributes"]
        if not isinstance(attrs, dict) or any(not isinstance(k, str) or not isinstance(v, list) or any(not isinstance(s, str) for s in v) for k, v in attrs.items()):
            raise ConfigError("Keycloak attributes require string lists")

    def observe(self, op: Operation) -> dict:
        self._check(op)
        groups = self._groups()
        matches = [g for g in groups if g.get("path") == op.resource]
        if not matches:
            return observation()
        if len(matches) != 1:
            raise BlockedError("Keycloak group path is ambiguous")
        group = matches[0]
        if not self.endpoint.identity.startswith("new:") and group["id"] != self.endpoint.identity:
            raise ConflictError("Keycloak immutable group identifier differs")
        return observation(group)

    def satisfied(self, op: Operation, current: dict) -> bool:
        self._check(op)
        return bool(current.get("exists") and current["body"].get("name") == op.desired["name"] and current["body"].get("attributes", {}) == op.desired["attributes"])

    def _version(self) -> str:
        version = self.endpoint.options.get("version")
        if not isinstance(version, str) or not version.startswith("26."):
            raise UnsupportedError("Keycloak writes require a pinned supported 26.x server version")
        info = self._request("GET", "/admin/serverinfo")
        if not isinstance(info, dict) or info.get("systemInfo", {}).get("version") != version:
            raise BlockedError("Keycloak server version is unverified or differs from spec")
        return version

    def apply(self, op: Operation, before: dict) -> dict:
        self._check(op)
        self._version()
        current = self.observe(op)
        if not same_observation(current, before):
            raise ConflictError("Keycloak group changed since review")
        if self.satisfied(op, current):
            return current
        if current["exists"]:
            raise ConflictError("existing Keycloak group differs; updates are unsupported")
        if self.endpoint.identity != "new:" + op.resource:
            raise BlockedError("new Keycloak group requires explicit new:/group identity")
        self._request("POST", self.realm_path + "/groups", json=op.desired)
        after = self.observe(op)
        if not self.satisfied(op, after):
            raise UnknownOutcome("Keycloak group creation is unconfirmed")
        return after

    def collect(self) -> dict:
        inv = empty_inventory(self.endpoint)
        inv["coverage"] = {key: "unknown" for key in ("identity", "identity_policies", "resource_policy", "boundary", "organization", "session", "resource_constraints")}
        inv["limitations"] = ["Realm collection is not proof of login, effective application access or external directory membership.",
                              "No user, external directory, protocol mapper, credential or composite-role changes are supported.",
                              "Keycloak lacks a conditional version field for group creation; existing groups are never updated."]
        try:
            realm = self._request("GET", self.realm_path)
            if not isinstance(realm, dict) or realm.get("realm") != self.endpoint.scope:
                raise BlockedError("Keycloak realm does not match scope")
            inv["evidence"].append({"realm": realm["realm"], "id": realm.get("id")})
            groups = self._groups()
            selected = [g for g in groups if g.get("path") == self.endpoint.resource]
            if selected and (len(selected) != 1 or (not self.endpoint.identity.startswith("new:") and selected[0]["id"] != self.endpoint.identity)):
                raise ConflictError("Keycloak immutable group identifier differs")
            inv["identities"] = [{"provider": "keycloak", "scope": self.endpoint.scope, "kind": "group", "stable_id": g["id"], "name": g.get("path", g.get("name"))} for g in groups]
            if selected:
                inv["coverage"]["identity"] = "complete"
                members = self._pages(self.realm_path + "/groups/" + quote(selected[0]["id"], safe="") + "/members", params={"briefRepresentation": "true"})
                inv["relationships"] = [{"kind": "group_membership", "group_id": selected[0]["id"], "user_id": m["id"]} for m in members]
            elif not self.endpoint.identity.startswith("new:"):
                inv["coverage"]["identity"] = "failed"
            users = self._pages(self.realm_path + "/users", params={"briefRepresentation": "true"})
            inv["identities"].extend({"provider": "keycloak", "scope": self.endpoint.scope, "kind": "user", "stable_id": u["id"], "name": u.get("username", u["id"])} for u in users)
            roles = self._pages(self.realm_path + "/roles")
            inv["policies"] = [{"kind": "realm_role", **{k: r[k] for k in ("id", "name", "composite", "clientRole", "containerId") if k in r}} for r in roles]
            # GET clients has no stable snapshot semantics; record only safe identifiers.
            clients = self._request("GET", self.realm_path + "/clients")
            if not isinstance(clients, list) or any(not isinstance(c, dict) or not c.get("id") for c in clients):
                raise BlockedError("Keycloak clients collection is incomplete")
            inv["evidence"].append({"clients": [{k: c[k] for k in ("id", "clientId", "protocol", "enabled") if k in c} for c in clients]})
            inv["policies"].extend({"kind": "group_role_mappings", **g} for g in groups)
            name = self.endpoint.resource.removeprefix("/")
            if "/" not in name:
                op = Operation("collect", "keycloak", "keycloak.group", self.endpoint.scope, self.endpoint.resource, {"name": name, "attributes": {}}, None)
                inv["observations"][observation_key(op.kind, op.resource)] = self.observe(op)
            inv["coverage"]["resource_policy"] = "complete"
            # Composite role expansion, client mappers and external sources are not complete.
            inv["coverage"]["identity_policies"] = "unknown"
        except (BlockedError, ConfigError, UnsupportedError) as exc:
            inv["coverage"]["resource_policy"] = "denied" if isinstance(exc, _Denied) else "failed"
            inv["evidence"].append("Keycloak realm collection incomplete; details withheld")
        return inv


def from_environment(endpoint: Endpoint) -> KeycloakAdapter:
    try:
        import httpx
    except ImportError:
        raise UnsupportedError("install identity-keycloak optional dependencies") from None
    token = os.environ.get("IDENTITY_KEYCLOAK_TOKEN")
    if not token:
        raise ConfigError("IDENTITY_KEYCLOAK_TOKEN environment variable is required")
    client = httpx.Client(headers={"Authorization": "Bearer " + token}, verify=True, timeout=30, follow_redirects=False)
    return KeycloakAdapter(endpoint, client)
