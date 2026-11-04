"""Namespace-only RBAC adapter. It never changes existing RBAC objects.

Creation uses API-server name uniqueness; existing resourceVersion is rechecked.
No cluster authentication, impersonation, or ClusterRoleBinding is configured here.
"""
from __future__ import annotations

import copy
import re
from typing import Any
from urllib.parse import urlparse

from .base import check_operation, empty_inventory, observation, observation_key, same_observation
from ..models import (BlockedError, ConfigError, ConflictError, Endpoint, Operation,
                      UnknownOutcome, UnsupportedError, canonical, fields)


KINDS = {"kubernetes.role", "kubernetes.role_binding"}
_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")
MAX_PAGES = 1000


def _dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise BlockedError("Kubernetes returned an invalid representation")


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status", None)


def _metadata(raw: dict) -> dict:
    meta = raw.get("metadata") or {}
    return {"name": meta.get("name"), "namespace": meta.get("namespace"), "uid": meta.get("uid"),
            "resourceVersion": meta.get("resourceVersion", meta.get("resource_version"))}


def _rules(rules: list[dict]) -> list[dict]:
    if not isinstance(rules, list) or not rules:
        raise ConfigError("Kubernetes Role requires explicit rules")
    result = []
    for raw in rules:
        if not isinstance(raw, dict):
            raise ConfigError("invalid Kubernetes rule")
        # Official client to_dict uses snake_case, API bodies use camelCase.
        rule = {"apiGroups": raw.get("apiGroups", raw.get("api_groups")),
                "resources": raw.get("resources"), "verbs": raw.get("verbs")}
        names = raw.get("resourceNames", raw.get("resource_names"))
        if names:
            rule["resourceNames"] = names
        if raw.get("nonResourceURLs", raw.get("non_resource_urls", raw.get("non_resource_ur_ls"))):
            raise UnsupportedError("non-resource Kubernetes permissions are unsupported")
        if any(k not in {"apiGroups", "api_groups", "resources", "verbs", "resourceNames", "resource_names", "nonResourceURLs", "non_resource_urls", "non_resource_ur_ls"} for k in raw):
            raise UnsupportedError("unsupported Kubernetes rule field")
        for key, values in rule.items():
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or "*" in v for v in values):
                raise UnsupportedError("Kubernetes rules require exact permission values")
            rule[key] = sorted(set(values))
        if any(verb not in {"get", "list", "watch"} for verb in rule["verbs"]):
            raise UnsupportedError("only namespace read permissions are supported")
        result.append(rule)
    return sorted(result, key=canonical)


class KubernetesAdapter:
    def __init__(self, endpoint: Endpoint, client=None, *, context_verified: bool = False, cluster: str | None = None):
        if endpoint.provider != "kubernetes" or not _NAME.fullmatch(endpoint.scope) or endpoint.resource != endpoint.scope:
            raise ConfigError("Kubernetes endpoint requires one explicit namespace")
        if not endpoint.options.get("context"):
            raise ConfigError("Kubernetes context must be explicit")
        parsed = urlparse(endpoint.options.get("url", ""))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError("Kubernetes requires a pinned HTTPS API server URL")
        self.endpoint, self.client = endpoint, client
        self.context_verified, self.cluster = context_verified, cluster

    def _check(self, op: Operation) -> str:
        check_operation(self.endpoint, op, KINDS)
        parts = op.resource.split("/")
        if len(parts) != 2 or parts[0] != self.endpoint.scope or not _NAME.fullmatch(parts[1]):
            raise BlockedError("Kubernetes operation escapes namespace")
        expected_name = self.endpoint.options.get("role_name", "migration-namespace-read") if op.kind == "kubernetes.role" else self.endpoint.options.get("role_binding_name", "migration-namespace-read")
        if parts[1] != expected_name:
            raise BlockedError("Kubernetes object not in endpoint allowlist")
        if op.kind == "kubernetes.role":
            fields(op.desired, {"rules"})
            _rules(op.desired["rules"])
        else:
            fields(op.desired, {"role_name", "group"})
            if op.desired["role_name"] != self.endpoint.options.get("role_name", "migration-namespace-read") or op.desired["group"] != self.endpoint.identity:
                raise BlockedError("Kubernetes binding subject or role is outside scope")
        return parts[1]

    def _clean(self, kind: str, value: Any) -> dict:
        raw = _dict(value)
        metadata = _metadata(raw)
        if metadata["namespace"] != self.endpoint.scope:
            raise BlockedError("Kubernetes returned another namespace")
        if not metadata["name"] or not metadata["uid"] or not metadata["resourceVersion"]:
            raise BlockedError("Kubernetes object lacks name, immutable identity or resourceVersion")
        if kind == "kubernetes.role":
            # Preserve unsupported existing rules as evidence; applying them is blocked.
            try:
                rules = _rules(raw.get("rules", []))
            except (UnsupportedError, ConfigError):
                rules = raw.get("rules", [])
            return {"metadata": metadata, "rules": rules}
        role_ref = raw.get("roleRef", raw.get("role_ref", {})) or {}
        ref = {"apiGroup": role_ref.get("apiGroup", role_ref.get("api_group")),
               "kind": role_ref.get("kind"), "name": role_ref.get("name")}
        subjects = []
        for subject in raw.get("subjects", []) or []:
            subjects.append({"apiGroup": subject.get("apiGroup", subject.get("api_group")),
                             "kind": subject.get("kind"), "name": subject.get("name"),
                             "namespace": subject.get("namespace")})
        return {"metadata": metadata, "roleRef": ref, "subjects": sorted(subjects, key=canonical)}

    def observe(self, op: Operation) -> dict:
        name = self._check(op)
        method = self.client.read_namespaced_role if op.kind == "kubernetes.role" else self.client.read_namespaced_role_binding
        try:
            body = self._clean(op.kind, method(name=name, namespace=self.endpoint.scope))
        except Exception as exc:
            if isinstance(exc, (BlockedError, ConfigError)):
                raise
            if _status(exc) == 404:
                return observation()
            raise BlockedError("Kubernetes object lookup failed") from None
        version = body["metadata"]["resourceVersion"]
        if body["metadata"]["name"] != name:
            raise BlockedError("Kubernetes returned a different object name")
        return observation(body, version)

    def satisfied(self, op: Operation, current: dict) -> bool:
        self._check(op)
        if not current.get("exists"):
            return False
        body = current["body"]
        if op.kind == "kubernetes.role":
            return body.get("rules") == _rules(op.desired["rules"])
        return body.get("roleRef") == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": op.desired["role_name"]} and body.get("subjects") == [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": op.desired["group"], "namespace": None}]

    def apply(self, op: Operation, before: dict) -> dict:
        name = self._check(op)
        if not self.context_verified or self.endpoint.options.get("group_claim_confirmed") is not True:
            raise BlockedError("Kubernetes context and actual group claim must be verified")
        current = self.observe(op)
        if not same_observation(current, before):
            raise ConflictError("Kubernetes resourceVersion or object changed")
        if self.satisfied(op, current):
            return current
        if current["exists"]:
            raise ConflictError("existing Kubernetes object differs; updates are unsupported")
        body = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role" if op.kind == "kubernetes.role" else "RoleBinding",
                "metadata": {"name": name, "namespace": self.endpoint.scope}}
        if op.kind == "kubernetes.role":
            body["rules"] = copy.deepcopy(op.desired["rules"])
            method = self.client.create_namespaced_role
        else:
            body["roleRef"] = {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": op.desired["role_name"]}
            body["subjects"] = [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": op.desired["group"]}]
            method = self.client.create_namespaced_role_binding
        try:
            method(namespace=self.endpoint.scope, body=body)
        except Exception as exc:
            if _status(exc) == 409:
                raise ConflictError("Kubernetes create conflicted") from None
            if _status(exc) in (400, 401, 403, 422):
                raise BlockedError("Kubernetes create rejected") from None
            raise UnknownOutcome("Kubernetes create outcome unknown; re-observe before resume") from None
        after = self.observe(op)
        if not self.satisfied(op, after):
            raise UnknownOutcome("Kubernetes created object is not confirmed")
        return after

    def _list(self, method, kind: str) -> list[dict]:
        result, token, seen, identities = [], None, set(), set()
        snapshot_version = None
        for _ in range(MAX_PAGES):
            kwargs = {"namespace": self.endpoint.scope, "limit": 100}
            if token:
                kwargs["_continue"] = token
            raw = _dict(method(**kwargs))
            if not isinstance(raw.get("items"), list):
                raise BlockedError("Kubernetes list response is incomplete")
            meta = raw.get("metadata") or {}
            version = meta.get("resourceVersion", meta.get("resource_version"))
            if snapshot_version is None:
                snapshot_version = version
            elif version != snapshot_version:
                raise BlockedError("Kubernetes pagination snapshot changed")
            for item in raw["items"]:
                clean = self._clean(kind, item)
                uid = clean["metadata"]["uid"]
                if not uid or uid in identities:
                    raise BlockedError("Kubernetes list contains missing or repeated immutable identifiers")
                identities.add(uid)
                result.append(clean)
            token = meta.get("continue", meta.get("_continue"))
            if not token:
                return result
            if not snapshot_version:
                raise BlockedError("Kubernetes paginated list lacks snapshot resourceVersion")
            if token in seen:
                raise BlockedError("Kubernetes pagination token repeated")
            seen.add(token)
        raise BlockedError("Kubernetes pagination exceeded the supported page limit")

    def collect(self) -> dict:
        inv = empty_inventory(self.endpoint)
        inv["coverage"] = {key: "unknown" for key in ("identity", "identity_policies", "resource_policy", "boundary", "organization", "session", "resource_constraints")}
        inv["identities"] = [{"provider": "kubernetes", "scope": self.endpoint.scope, "name": self.endpoint.identity,
                              "stable_id": self.endpoint.identity, "kind": "group_claim"}]
        inv["coverage"]["identity"] = "complete" if self.context_verified and self.endpoint.options.get("group_claim_confirmed") is True else "unknown"
        inv["limitations"] = ["Group claim is an asserted external identity; login and membership were not tested.",
                              "ClusterRoleBindings and overlapping user/group privileges remain unknown.",
                              "Existing RBAC resources are never updated or removed."]
        if self.cluster:
            inv["evidence"].append({"context": self.endpoint.options["context"], "cluster": self.cluster})
        try:
            roles = self._list(self.client.list_namespaced_role, "kubernetes.role")
            bindings = self._list(self.client.list_namespaced_role_binding, "kubernetes.role_binding")
            inv["policies"] = [{"kind": "kubernetes.role", **role} for role in roles]
            inv["relationships"] = [{"kind": "resource_role_binding", **binding} for binding in bindings]
            inv["coverage"]["identity_policies"] = "complete"
            inv["coverage"]["resource_policy"] = "complete"
            for kind, name in (("kubernetes.role", self.endpoint.options.get("role_name", "migration-namespace-read")),
                               ("kubernetes.role_binding", self.endpoint.options.get("role_binding_name", "migration-namespace-read"))):
                desired = {"rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}]} if kind == "kubernetes.role" else {"role_name": self.endpoint.options.get("role_name", "migration-namespace-read"), "group": self.endpoint.identity}
                op = Operation("collect", "kubernetes", kind, self.endpoint.scope, f"{self.endpoint.scope}/{name}", desired, None)
                inv["observations"][observation_key(kind, op.resource)] = self.observe(op)
        except Exception as exc:
            status = "denied" if _status(exc) in (401, 403) else "failed"
            inv["coverage"]["identity_policies"] = status
            inv["coverage"]["resource_policy"] = status
            inv["evidence"].append("Kubernetes scoped RBAC collection incomplete")
        return inv


KubeAdapter = KubernetesAdapter


def from_environment(endpoint: Endpoint) -> KubernetesAdapter:
    try:
        from kubernetes import client, config
    except ImportError:
        raise UnsupportedError("install identity-k8s optional dependencies") from None
    context_name = endpoint.options.get("context")
    if not context_name:
        raise ConfigError("Kubernetes context must be explicit")
    try:
        contexts, _ = config.list_kube_config_contexts()
        selected = [c for c in contexts or [] if c.get("name") == context_name]
        if len(selected) != 1:
            raise ConfigError("Kubernetes context is missing or ambiguous")
        context = selected[0]["context"]
        if context.get("namespace", "default") != endpoint.scope:
            raise BlockedError("Kubernetes context namespace differs from spec")
        configuration = client.Configuration()
        config.load_kube_config(context=context_name, client_configuration=configuration, persist_config=False)
        configuration.retries = 0
        if configuration.verify_ssl is not True:
            raise BlockedError("Kubernetes certificate validation is required")
        if configuration.host.rstrip("/") != endpoint.options.get("url", "").rstrip("/"):
            raise BlockedError("Kubernetes context API server differs from pinned URL")
        api_client = client.ApiClient(configuration=configuration)
        return KubernetesAdapter(endpoint, client.RbacAuthorizationV1Api(api_client), context_verified=True, cluster=context["cluster"])
    except (ConfigError, BlockedError):
        raise
    except Exception:
        raise BlockedError("Kubernetes context initialization failed") from None
