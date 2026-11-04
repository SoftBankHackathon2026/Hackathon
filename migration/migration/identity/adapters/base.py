"""Provider contract. All observations: {exists: bool, body: dict, version: str|None}."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Protocol

from ..models import Endpoint, Operation, UnsupportedError, state_digest, primitive


class Adapter(Protocol):
    endpoint: Endpoint

    def collect(self) -> dict: ...
    def observe(self, operation: Operation) -> dict: ...
    def apply(self, operation: Operation, before: dict) -> dict: ...
    def satisfied(self, operation: Operation, observation: dict) -> bool: ...


def observation(body: dict | None = None, version: str | None = None) -> dict:
    return {"exists": body is not None, "body": body if body is not None else {}, "version": version}


def observation_key(kind: str, resource: str) -> str:
    return f"{kind}|{resource}"


def empty_inventory(endpoint: Endpoint) -> dict:
    return {"endpoint": primitive(endpoint), "identities": [], "policies": [], "trust": [],
            "coverage": {}, "observations": {}, "relationships": [], "evidence": [], "limitations": [],
            "collected_at": datetime.now(timezone.utc).isoformat()}


def check_operation(endpoint: Endpoint, op: Operation, kinds: set[str]) -> None:
    if op.provider != endpoint.provider or op.scope != endpoint.scope or op.kind not in kinds:
        raise UnsupportedError("operation outside adapter scope or unsupported kind")


def same_observation(left: dict, right: dict) -> bool:
    return state_digest(left) == state_digest(right)
