"""Keystone v3 scoped collection and explicit additive group-role assignment.

Federation configuration is collected as evidence, never automatically translated.
Service policy semantics and effective inherited authority remain unknown.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from .base import check_operation, empty_inventory, observation, observation_key, same_observation
from ..models import (BlockedError, ConfigError, ConflictError, Endpoint, Operation,
                      UnknownOutcome, UnsupportedError, fields, reject_secrets)

MAX_ASSIGNMENTS = 100000

def _dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise BlockedError("OpenStack response has invalid representation")


def _http_status(exc) -> int | None:
    return getattr(exc, "http_status", getattr(exc, "status_code", None))


class OpenStackAdapter:
    def __init__(self, endpoint: Endpoint, client=None):
        if endpoint.provider != "openstack" or endpoint.resource != endpoint.scope:
            raise ConfigError("OpenStack endpoint requires one project identifier")
        if not endpoint.options.get("domain_id"):
            raise ConfigError("OpenStack domain_id is required")
        self.endpoint, self.client = endpoint, client
        self.identity = getattr(client, "identity", client)

    def _context(self) -> tuple[dict, dict]:
        try:
            project_id = getattr(self.client, "current_project_id", None)
            if callable(project_id):
                project_id = project_id()
            if project_id != self.endpoint.scope:
                raise BlockedError("OpenStack authenticated project differs from spec")
            project = _dict(self.identity.get_project(self.endpoint.scope))
            group = _dict(self.identity.get_group(self.endpoint.identity))
            if project.get("id") != self.endpoint.scope or group.get("id") != self.endpoint.identity:
                raise BlockedError("OpenStack immutable group or project identifier differs")
            domain = self.endpoint.options["domain_id"]
            if project.get("domain_id") != domain or group.get("domain_id") != domain:
                raise BlockedError("OpenStack group or project domain differs from spec")
            if project.get("is_enabled", project.get("enabled", True)) is not True:
                raise BlockedError("OpenStack project is disabled")
            return project, group
        except (BlockedError, ConfigError):
            raise
        except Exception:
            raise BlockedError("OpenStack project or group context could not be verified") from None

    def _check(self, op: Operation) -> None:
        check_operation(self.endpoint, op, {"openstack.role_assignment"})
        fields(op.desired, {"group_id", "role_id", "project_id"})
        if op.resource != self.endpoint.scope or op.desired["project_id"] != self.endpoint.scope or op.desired["group_id"] != self.endpoint.identity or op.desired["role_id"] != self.endpoint.options.get("role_id"):
            raise BlockedError("OpenStack assignment is outside explicit project/group/role scope")

    def observe(self, op: Operation) -> dict:
        self._check(op)
        self._context()
        try:
            role = _dict(self.identity.get_role(op.desired["role_id"]))
            if role.get("id") != op.desired["role_id"]:
                raise BlockedError("OpenStack role identifier differs")
            role_domain = role.get("domain_id")
            if role_domain and role_domain != self.endpoint.options["domain_id"]:
                raise BlockedError("OpenStack role belongs to another domain")
            present = self.identity.validate_group_has_project_role(self.endpoint.scope, self.endpoint.identity, op.desired["role_id"], inherited=False)
            if type(present) is not bool:
                raise BlockedError("OpenStack role assignment response is ambiguous")
            return observation(dict(op.desired)) if present else observation()
        except BlockedError:
            raise
        except Exception:
            raise BlockedError("OpenStack explicit assignment lookup failed") from None

    def satisfied(self, op: Operation, current: dict) -> bool:
        self._check(op)
        return bool(current.get("exists") and current.get("body") == op.desired)

    def apply(self, op: Operation, before: dict) -> dict:
        self._check(op)
        current = self.observe(op)
        if not same_observation(current, before):
            raise ConflictError("OpenStack explicit assignment changed since review")
        if self.satisfied(op, current):
            return current
        try:
            self.identity.assign_project_role_to_group(self.endpoint.scope, self.endpoint.identity, op.desired["role_id"], inherited=False)
        except Exception as exc:
            if _http_status(exc) in (400, 401, 403, 404):
                raise BlockedError("OpenStack role assignment rejected") from None
            raise UnknownOutcome("OpenStack role assignment outcome unknown; re-observe before resume") from None
        after = self.observe(op)
        if not self.satisfied(op, after):
            raise UnknownOutcome("OpenStack role assignment is unconfirmed")
        return after

    def collect(self) -> dict:
        inv = empty_inventory(self.endpoint)
        inv["coverage"] = {key: "unknown" for key in ("identity", "identity_policies", "resource_policy", "boundary", "organization", "session", "resource_constraints")}
        inv["limitations"] = ["Keystone group role assignment does not establish service-policy equivalence or user login.",
                              "Federation protocol/mapping automatic conversion is unsupported.",
                              "Keystone role assignments lack conditional versions; only explicit additive grants are supported."]
        try:
            project, group = self._context()
            inv["identities"] = [{"provider": "openstack", "scope": self.endpoint.scope, "stable_id": group["id"],
                                  "kind": "group", "name": group.get("name", group["id"]), "domain_id": self.endpoint.options["domain_id"]}]
            inv["evidence"].append({"project_id": project["id"], "domain_id": project["domain_id"]})
            inv["coverage"]["identity"] = "complete"
            # SDK generator traverses all service-provided pagination links.
            assignments = self.identity.role_assignments(group_id=self.endpoint.identity, scope_project_id=self.endpoint.scope)
            for count, value in enumerate(assignments):
                if count >= MAX_ASSIGNMENTS:
                    raise BlockedError("OpenStack role assignments exceeded the supported size limit")
                raw = _dict(value)
                clean = {k: raw[k] for k in ("group", "role", "scope", "user") if raw.get(k) is not None}
                reject_secrets(clean)
                if clean.get("group", {}).get("id") != self.endpoint.identity:
                    raise BlockedError("OpenStack role-assignment query returned another principal")
                if clean.get("scope", {}).get("project", {}).get("id") != self.endpoint.scope:
                    raise BlockedError("OpenStack role-assignment query returned another project")
                inv["relationships"].append({"kind": "project_role_assignment", **clean})
            inv["coverage"]["resource_policy"] = "complete"
            role_id = self.endpoint.options.get("role_id")
            if role_id:
                op = Operation("collect", "openstack", "openstack.role_assignment", self.endpoint.scope, self.endpoint.scope,
                               {"group_id": self.endpoint.identity, "role_id": role_id, "project_id": self.endpoint.scope}, None)
                inv["observations"][observation_key(op.kind, op.resource)] = self.observe(op)
            idp = self.endpoint.options.get("identity_provider_id")
            protocol = self.endpoint.options.get("protocol_id")
            mapping = self.endpoint.options.get("mapping_id")
            if idp:
                raw = _dict(self.identity.get_identity_provider(idp))
                if raw.get("id") != idp:
                    raise BlockedError("OpenStack identity-provider identifier differs")
                inv["trust"].append({"kind": "identity_provider", **{k: raw[k] for k in ("id", "domain_id", "remote_ids", "is_enabled") if k in raw}})
            if protocol:
                if not idp:
                    raise ConfigError("federation protocol requires identity_provider_id")
                raw = _dict(self.identity.get_federation_protocol(idp, protocol))
                inv["trust"].append({"kind": "federation_protocol", **{k: raw[k] for k in ("id", "mapping_id", "identity_provider_id") if k in raw}})
            if mapping:
                raw = _dict(self.identity.get_mapping(mapping))
                clean = {k: raw[k] for k in ("id", "rules") if k in raw}
                reject_secrets(clean)
                inv["policies"].append({"kind": "federation_mapping", **clean})
        except (BlockedError, ConfigError, UnsupportedError):
            inv["coverage"]["resource_policy"] = "failed"
            inv["evidence"].append("OpenStack scoped collection incomplete; details withheld")
        except Exception as exc:
            inv["coverage"]["resource_policy"] = "denied" if _http_status(exc) in (401, 403) else "failed"
            inv["evidence"].append("OpenStack scoped collection incomplete; details withheld")
        return inv


def from_environment(endpoint: Endpoint) -> OpenStackAdapter:
    try:
        import openstack
    except ImportError:
        raise UnsupportedError("install identity-openstack optional dependencies") from None
    auth_url = os.environ.get("OS_AUTH_URL", "")
    parsed = urlparse(auth_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("OpenStack OS_AUTH_URL must be HTTPS")
    if endpoint.options.get("url", "").rstrip("/") != auth_url.rstrip("/"):
        raise BlockedError("OpenStack authentication URL differs from the explicitly reviewed endpoint")
    if os.environ.get("OS_INSECURE", "").lower() in {"true", "1", "yes"}:
        raise ConfigError("OpenStack certificate verification is required")
    # Do not choose an ambient named cloud or persist authentication artifacts.
    if os.environ.get("OS_CLOUD"):
        raise ConfigError("use explicit OS_* authentication variables instead of OS_CLOUD")
    try:
        connection = openstack.connect(cloud=None, verify=True, auth_url=auth_url, api_timeout=30)
        adapter = OpenStackAdapter(endpoint, connection)
        adapter._context()
        return adapter
    except (ConfigError, BlockedError):
        raise
    except Exception:
        raise BlockedError("OpenStack authentication context initialization failed") from None
