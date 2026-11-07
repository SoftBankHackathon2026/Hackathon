from __future__ import annotations

import re
from pathlib import Path

from ..identity.common import (
    IdentityError,
    https_url,
    identifier,
    keys,
    require,
    string,
)
from ..secrets.common import SecretsError, read_file


def secret_file(path):
    require(isinstance(path, str) and Path(path).is_absolute(), "SECRET_FILE_REFERENCE")
    try:
        value = read_file(path, private=True, limit=16384).decode().strip()
    except SecretsError as error:
        raise IdentityError(error.code) from None
    except UnicodeError:
        raise IdentityError("SECRET_UNAVAILABLE") from None
    require(bool(value) and all(32 < ord(c) < 127 for c in value), "SECRET_UNAVAILABLE")
    return value


def account_id(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{12}", value), "ACCOUNT_ID")
    return value


def absolute_file(value):
    require(Path(string(value)).is_absolute(), "ABSOLUTE_FILE_REQUIRED")
    return value


def validate_config(c):
    fields = {"schema_version", "migration_id", "source", "target"}
    keys(c, fields, fields)
    require(
        type(c["schema_version"]) is int and c["schema_version"] == 1, "SCHEMA_VERSION"
    )
    identifier(c["migration_id"])
    s = c["source"]
    keys(
        s,
        {"provider", "base_url", "realm", "client_id", "client_secret_file", "ca_file"},
        {"provider", "base_url", "realm", "client_id", "client_secret_file"},
    )
    require(s["provider"] == "keycloak", "UNSUPPORTED_SOURCE")
    https_url(s["base_url"])
    identifier(s["realm"])
    require(s["realm"] != "master", "BUSINESS_REALM_REQUIRED")
    identifier(s["client_id"])
    absolute_file(s["client_secret_file"])
    if "ca_file" in s:
        absolute_file(s["ca_file"])
    t = c["target"]
    keys(
        t,
        {
            "provider",
            "region",
            "profile",
            "instance_arn",
            "identity_store_id",
            "account_ids",
        },
        {"provider", "region", "instance_arn", "identity_store_id", "account_ids"},
    )
    require(t["provider"] == "aws-identity-center", "UNSUPPORTED_TARGET")
    for field in ("region", "instance_arn", "identity_store_id"):
        string(t[field])
    require(t["instance_arn"].startswith("arn:"), "INSTANCE_ARN")
    if "profile" in t:
        string(t["profile"])
    accounts = t["account_ids"]
    require(isinstance(accounts, list) and accounts, "ACCOUNT_SCOPE_REQUIRED")
    for a in accounts:
        account_id(a)
    require(len(accounts) == len(set(accounts)), "DUPLICATE_ACCOUNT_SCOPE")
    return c


def owner(config):
    return "identity-export:" + config["migration_id"]


def primary_email(emails):
    values = [e for e in emails or [] if isinstance(e, dict) and e.get("Value")]
    chosen = next(
        (e for e in values if e.get("Primary")), values[0] if values else None
    )
    return chosen["Value"].lower() if chosen else None
