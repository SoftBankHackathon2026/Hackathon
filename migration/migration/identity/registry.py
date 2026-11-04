"""Explicit live factories. Importing the CLI never discovers credentials."""
from importlib import import_module
from .models import Endpoint, UnsupportedError


def from_environment(endpoint: Endpoint):
    if endpoint.provider not in {"aws", "gcp", "keycloak", "kubernetes", "openstack"}:
        raise UnsupportedError("provider has no live adapter")
    return import_module(f"migration.identity.adapters.{endpoint.provider}").from_environment(endpoint)
