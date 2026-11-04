"""Versioned, JSON-safe contracts shared by planners and provider adapters."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import RULE_VERSION, SCHEMA_VERSION


class IdentityError(Exception):
    """Safe public error: never wrap raw provider exception text."""
    exit_code = 1


class ConfigError(IdentityError):
    exit_code = 2


class BlockedError(IdentityError):
    exit_code = 4


class ConflictError(BlockedError):
    pass


class UnsupportedError(BlockedError):
    pass


class UnknownOutcome(IdentityError):
    exit_code = 6


def primitive(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return primitive(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): primitive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [primitive(v) for v in value]
    if value is None or type(value) in (str, int, bool, float):
        return value
    raise ConfigError("non-JSON value in artifact")


def canonical(value: Any) -> str:
    return json.dumps(primitive(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def ordered_evidence(value: Any, key: str = "") -> Any:
    """Sort sets in supported policy formats; do not reorder federation mappings."""
    if isinstance(value, dict):
        return {k: ordered_evidence(v, k) for k, v in value.items()}
    if isinstance(value, list):
        result = [ordered_evidence(v) for v in value]
        if key in {"identities", "policies", "trust", "relationships", "Statement", "Action", "Resource",
                   "bindings", "members", "included_permissions", "includedPermissions", "roles", "subjects"}:
            result.sort(key=canonical)
        return result
    return value


def state_digest(value: Any) -> str:
    return digest(ordered_evidence(value))


_SECRET_KEYS = {"password", "token", "access_token", "refresh_token", "private_key", "client_secret",
                "secret_access_key", "aws_secret_access_key", "authorization", "cookie", "credentials"}


def reject_secrets(value: Any) -> None:
    """Reject secret fields rather than silently changing a signed artifact."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower().replace("-", "_") in _SECRET_KEYS:
                raise ConfigError("secret-bearing field is prohibited")
            reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str) and ("-----BEGIN " in value or re.search(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", value)):
        raise ConfigError("credential material is prohibited")


def fields(raw: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(raw, dict) or set(raw) - required - optional or required - set(raw):
        raise ConfigError("unknown or missing fields")
    reject_secrets(raw)
    return raw


def string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(f"invalid {label}")
    return value


PROVIDERS = {"aws", "gcp", "keycloak", "kubernetes", "openstack"}
OPTION_KEYS = {"stable_id", "role_id", "role_name", "policy_name", "service_account_id", "trust_policy",
               "group", "role_binding_name", "namespace", "url", "version", "context", "domain_id",
               "project_id", "group_id", "protocol_id", "mapping_id", "identity_provider_id",
               "project_number", "pool_id", "provider_id", "federation_mode", "attribute_mapping",
               "attribute_condition", "execution_identity_confirmed", "group_claim_confirmed",
               "region", "profile", "auth_env", "client_id"}


@dataclass(frozen=True)
class Endpoint:
    provider: str
    scope: str
    identity: str
    resource: str
    options: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> Endpoint:
        fields(raw, {"provider", "scope", "identity", "resource"}, {"options"})
        if not isinstance(raw["provider"], str) or raw["provider"] not in PROVIDERS:
            raise ConfigError("unsupported provider")
        for key in ("scope", "identity", "resource"):
            string(raw[key], key)
        options = raw.get("options", {})
        fields(options, set(), OPTION_KEYS)
        for key in ("execution_identity_confirmed", "group_claim_confirmed"):
            if key in options and type(options[key]) is not bool:
                raise ConfigError(f"{key} requires a boolean")
        for key, value in options.items():
            if key in {"execution_identity_confirmed", "group_claim_confirmed"}:
                continue
            if key in {"trust_policy", "attribute_mapping"}:
                if not isinstance(value, dict):
                    raise ConfigError(f"{key} requires a mapping")
            elif not isinstance(value, str) or not value:
                raise ConfigError(f"{key} requires a nonempty string")
        return cls(**raw)


@dataclass(frozen=True)
class MigrationSpec:
    schema_version: int
    direction: str
    workload: str
    source: Endpoint
    target: Endpoint
    allowed_changes: list[str]
    verification: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> MigrationSpec:
        fields(raw, {"schema_version", "direction", "workload", "source", "target", "allowed_changes"}, {"verification"})
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise ConfigError("unsupported schema version")
        for key in ("direction", "workload"):
            string(raw[key], key)
        changes = raw["allowed_changes"]
        if not isinstance(changes, list) or any(not isinstance(x, str) or not x for x in changes) or len(set(changes)) != len(changes):
            raise ConfigError("allowed_changes requires unique operation kinds")
        verification = raw.get("verification", [])
        if not isinstance(verification, list) or any(not isinstance(v, dict) for v in verification):
            raise ConfigError("invalid verification cases")
        case_ids = set()
        for case in verification:
            fields(case, {"id", "action", "resource", "expect"}, {"object", "isolated"})
            for key in ("id", "action", "resource", "expect"):
                string(case[key], f"verification {key}")
            if case["id"] in case_ids or case["action"] not in {"list", "get", "put", "delete", "delegate", "login", "group-removal"} or case["expect"] not in {"allowed", "denied"}:
                raise ConfigError("unsupported or duplicate verification case")
            if "isolated" in case and type(case["isolated"]) is not bool:
                raise ConfigError("verification isolation requires a boolean")
            if "object" in case:
                string(case["object"], "verification object")
            case_ids.add(case["id"])
        return cls(raw["schema_version"], raw["direction"], raw["workload"], Endpoint.from_dict(raw["source"]),
                   Endpoint.from_dict(raw["target"]), changes, verification)


@dataclass(frozen=True)
class Operation:
    id: str
    provider: str
    kind: str
    scope: str
    resource: str
    desired: dict
    expected: dict | None
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict) -> Operation:
        fields(raw, {"id", "provider", "kind", "scope", "resource", "desired", "expected"}, {"depends_on"})
        for key in ("id", "provider", "kind", "scope", "resource"):
            string(raw[key], key)
        if not isinstance(raw["desired"], dict) or (raw["expected"] is not None and not isinstance(raw["expected"], dict)):
            raise ConfigError("invalid operation state")
        if not isinstance(raw.get("depends_on", []), list) or any(not isinstance(v, str) for v in raw.get("depends_on", [])):
            raise ConfigError("invalid dependencies")
        return cls(**raw)


@dataclass(frozen=True)
class Inventory:
    schema_version: int
    spec_digest: str
    source: dict
    target: dict

    @classmethod
    def from_dict(cls, raw: dict) -> Inventory:
        fields(raw, {"schema_version", "spec_digest", "source", "target"})
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
            raise ConfigError("unsupported inventory schema")
        string(raw["spec_digest"], "spec digest")
        for side in (raw["source"], raw["target"]):
            fields(side, {"endpoint", "identities", "policies", "trust", "coverage", "observations"},
                   {"relationships", "evidence", "collected_at", "limitations"})
            Endpoint.from_dict(side["endpoint"])
            for key in ("identities", "policies", "trust"):
                if not isinstance(side[key], list) or any(not isinstance(x, dict) for x in side[key]):
                    raise ConfigError("invalid inventory list")
            for identity in side["identities"]:
                for key in {"provider", "scope", "id", "stable_id", "name", "arn"}.intersection(identity):
                    string(identity[key], f"identity {key}")
            if not isinstance(side["coverage"], dict) or not isinstance(side["observations"], dict):
                raise ConfigError("invalid inventory coverage or observations")
            if any(not isinstance(v, str) or v not in {"complete", "not_applicable", "unknown", "denied", "failed", "unsupported"} for v in side["coverage"].values()):
                raise ConfigError("invalid coverage status")
            for value in side["observations"].values():
                fields(value, {"exists", "body", "version"})
                if type(value["exists"]) is not bool or not isinstance(value["body"], dict) or (value["version"] is not None and not isinstance(value["version"], str)) or (not value["exists"] and value["body"]):
                    raise ConfigError("invalid observation")
            if not isinstance(side.get("relationships", []), list) or any(not isinstance(x, dict) for x in side.get("relationships", [])):
                raise ConfigError("invalid relationships")
        return cls(**raw)


@dataclass(frozen=True)
class Plan:
    schema_version: int
    rule_version: str
    spec: MigrationSpec
    inventory: Inventory
    operations: list[Operation]
    blockers: list[str]
    warnings: list[str]
    graph: dict
    plan_digest: str = ""

    def payload(self) -> dict:
        value = primitive(self)
        value.pop("plan_digest")
        return value

    def sealed(self) -> Plan:
        return dataclasses.replace(self, plan_digest=digest(self.payload()))

    @classmethod
    def from_dict(cls, raw: dict) -> Plan:
        fields(raw, {"schema_version", "rule_version", "spec", "inventory", "operations", "blockers", "warnings", "graph", "plan_digest"})
        if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION or raw["rule_version"] != RULE_VERSION:
            raise ConfigError("unsupported plan or rule version")
        if not isinstance(raw["operations"], list) or not isinstance(raw["graph"], dict):
            raise ConfigError("invalid plan operations or graph")
        for key in ("blockers", "warnings"):
            if not isinstance(raw[key], list) or any(not isinstance(x, str) for x in raw[key]):
                raise ConfigError("invalid plan diagnostics")
        value = cls(raw["schema_version"], raw["rule_version"], MigrationSpec.from_dict(raw["spec"]),
                    Inventory.from_dict(raw["inventory"]), [Operation.from_dict(o) for o in raw["operations"]],
                    raw["blockers"], raw["warnings"], raw["graph"], raw["plan_digest"])
        if value.sealed().plan_digest != value.plan_digest:
            raise ConfigError("plan digest mismatch")
        return value
