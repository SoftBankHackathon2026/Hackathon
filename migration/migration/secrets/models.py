"""Metadata-only public models. Payloads are deliberately not serializable reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class MigrationError(Exception):
    """Only constant, locally authored error codes may cross the output boundary."""

    def __init__(self, code: str = "operation_failed"):
        # Never propagate provider exception text or payloads.
        self.code = code if code in ERROR_CODES else "operation_failed"
        super().__init__(self.code)


ERROR_CODES = frozenset(
    {
        "operation_failed",
        "invalid_config",
        "dependency_missing",
        "authentication_failed",
        "access_denied",
        "not_found",
        "unavailable",
        "conflict",
        "unsupported",
        "blocked",
        "unknown_write",
        "verification_failed",
        "plan_changed",
        "locked",
        "unsafe_path",
        "recovery_required",
        "freeze_expired",
        "application_changed",
        "identity_mismatch",
        "needs_unseal",
        "needs_credentials",
        "needs_recovery",
        "failed",
        "retention_insufficient",
        "invalid_state",
        "policy_unavailable",
        "policy_denied",
    }
)


class Support(StrEnum):
    DIRECT = "direct"
    TRANSFORM = "transform"
    ADDITIONAL = "additional"
    UNSUPPORTED = "unsupported"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class Capability:
    feature: str
    support: Support
    reason: str
    executable: bool = False


@dataclass(frozen=True, repr=False)
class SecretValue:
    data: bytes = field(repr=False)
    kind: str = "binary"

    def __post_init__(self):
        if not isinstance(self.data, bytes) or self.kind not in {
            "string",
            "binary",
            "json",
        }:
            raise MigrationError("invalid_state")

    def __repr__(self):
        return "<SecretValue [REDACTED]>"

    def __str__(self):
        return "[REDACTED]"


@dataclass(frozen=True)
class Version:
    id: str
    state: str = "enabled"
    # AWS stages/GCP aliases normalized to current, previous, pending; custom names kept.
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class Resource:
    id: str
    name: str
    versions: tuple[Version, ...]
    features: tuple[str, ...] = ()


@dataclass(frozen=True)
class Inventory:
    identity: (
        str  # authoritative backend identity incl. namespace/mount, never credential
    )
    provider: str
    resources: tuple[Resource, ...]
    capabilities: tuple[Capability, ...]
    healthy: bool = True


@dataclass(frozen=True)
class WriteReceipt:
    version_id: str


class ReconcileStatus(StrEnum):
    FOUND = "found"
    ABSENT = "absent"  # authoritative proof request did not execute, retry allowed
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReconcileResult:
    status: ReconcileStatus
    version_id: str | None = None


@dataclass(frozen=True)
class CopyItem:
    source_resource: str
    source_name: str
    source_version: str
    target_name: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    id: str
    source_identity: str
    target_identity: str
    items: tuple[CopyItem, ...]
    capabilities: tuple[Capability, ...]
    blockers: tuple[str, ...]
    config_digest: str

    @property
    def allowed(self) -> bool:
        return not self.blockers
