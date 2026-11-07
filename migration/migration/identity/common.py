"""Private artifacts, canonical hashes, strict input and non-secret errors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml


class IdentityError(Exception):
    def __init__(self, code, *, status="blocked"):
        self.code = code
        self.status = status
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise IdentityError(code)


def keys(value, allowed, required=()):
    require(isinstance(value, dict), "OBJECT_REQUIRED")
    require(not set(value) - set(allowed), "UNKNOWN_FIELD")
    require(set(required) <= set(value), "MISSING_FIELD")


def string(value):
    require(isinstance(value, str) and 0 < len(value) <= 2048, "STRING_REQUIRED")
    require(not any(ord(c) < 32 for c in value), "INVALID_STRING")
    return value


def identifier(value):
    string(value)
    require(
        bool(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,126}", value)), "INVALID_ID"
    )
    return value


def https_url(value):
    string(value)
    try:
        p = urlsplit(value)
        valid = p.scheme == "https" and p.hostname and p.port != 0
    except ValueError:
        valid = False
    require(
        valid and not p.username and not p.password and not p.query and not p.fragment,
        "HTTPS_URL_REQUIRED",
    )
    require(not any(c.isspace() for c in value) and "\\" not in value, "INVALID_URL")
    return value.rstrip("/")


def secret(name):
    require(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name),
        "INVALID_SECRET_REFERENCE",
    )
    value = os.environ.get(name)
    require(
        bool(value) and "\n" not in value and "\r" not in value, "SECRET_UNAVAILABLE"
    )
    return value


def now():
    return datetime.now(UTC).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stable_id(*parts):
    return digest(parts)[:24]


def load(path):
    try:

        def unique(loader, node, deep=False):
            result = {}
            for k, v in node.value:
                key = loader.construct_object(k, deep=deep)
                require(
                    isinstance(key, str) and key not in result,
                    "DUPLICATE_OR_INVALID_KEY",
                )
                result[key] = loader.construct_object(v, deep=deep)
            return result

        class Loader(yaml.SafeLoader):
            pass

        Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique)
        value = yaml.load(Path(path).read_text(), Loader=Loader)
        require(isinstance(value, dict), "OBJECT_REQUIRED")
        return value
    except (OSError, ValueError, yaml.YAMLError, TypeError):
        raise IdentityError("INPUT_UNREADABLE") from None


def private_write(path, value):
    private_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def private_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(value)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def validate_config(c):
    keys(
        c,
        {"schema_version", "migration_id", "source", "targets"},
        {"schema_version", "migration_id", "source", "targets"},
    )
    require(
        type(c["schema_version"]) is int and c["schema_version"] == 1, "SCHEMA_VERSION"
    )
    identifier(c["migration_id"])
    s = c["source"]
    keys(
        s,
        {
            "provider",
            "region",
            "profile",
            "instance_arn",
            "identity_store_id",
            "account_ids",
            "account_profiles",
        },
        {"provider", "region", "instance_arn", "identity_store_id", "account_ids"},
    )
    require(s["provider"] == "aws-identity-center", "UNSUPPORTED_SOURCE")
    for field in ("region", "instance_arn", "identity_store_id"):
        string(s[field])
    accounts = s["account_ids"]
    require(
        isinstance(accounts, list) and accounts,
        "ACCOUNT_SCOPE_REQUIRED",
    )
    require(
        all(isinstance(a, str) and re.fullmatch(r"\d{12}", a) for a in accounts),
        "ACCOUNT_ID",
    )
    require(len(accounts) == len(set(accounts)), "DUPLICATE_ACCOUNT_SCOPE")
    profiles = s.get("account_profiles", {})
    require(
        isinstance(profiles, dict) and not set(profiles) - set(accounts),
        "ACCOUNT_PROFILES",
    )
    for v in profiles.values():
        string(v)
    if "profile" in s:
        string(s["profile"])
    t = c["targets"]
    keys(t, {"keycloak", "openbao", "kubernetes", "applications"}, {"keycloak"})
    kc = t["keycloak"]
    keys(
        kc,
        {"base_url", "realm", "admin_token_env", "ca_file", "infra_result"},
        {"base_url", "realm", "admin_token_env"},
    )
    https_url(kc["base_url"])
    identifier(kc["realm"])
    require(kc["realm"] != "master", "BUSINESS_REALM_REQUIRED")
    identifier(kc["admin_token_env"])
    if "openbao" in t:
        b = t["openbao"]
        keys(
            b,
            {"base_url", "token_env", "ca_file", "auth_mount", "audience"},
            {"base_url", "token_env", "auth_mount", "audience"},
        )
        https_url(b["base_url"])
        identifier(b["token_env"])
        identifier(b["audience"])
        require(
            bool(re.fullmatch(r"identity-[a-z0-9-]+", b["auth_mount"])),
            "DEDICATED_AUTH_MOUNT",
        )
    if "kubernetes" in t:
        k = t["kubernetes"]
        keys(
            k,
            {"kubeconfig", "context", "group_prefix", "audience", "redirect_uris"},
            {"kubeconfig", "context", "group_prefix", "audience", "redirect_uris"},
        )
        require(Path(string(k["kubeconfig"])).is_absolute(), "ABSOLUTE_KUBECONFIG")
        string(k["context"])
        identifier(k["audience"])
        require(
            isinstance(k["group_prefix"], str)
            and k["group_prefix"]
            and not k["group_prefix"].startswith("system:"),
            "GROUP_PREFIX_REQUIRED",
        )
        redirects(k["redirect_uris"])
    apps = t.get("applications", [])
    require(isinstance(apps, list), "APPLICATION_LIST")
    names = set()
    for app in apps:
        keys(
            app,
            {"id", "client_id", "redirect_uris"},
            {"id", "client_id", "redirect_uris"},
        )
        identifier(app["id"])
        identifier(app["client_id"])
        require(app["id"] not in names, "DUPLICATE_APPLICATION")
        names.add(app["id"])
        redirects(app["redirect_uris"])
    for target in (kc, t.get("openbao", {})):
        if "ca_file" in target:
            require(Path(string(target["ca_file"])).is_absolute(), "ABSOLUTE_CA_FILE")
    return c


def redirects(values):
    require(isinstance(values, list) and values, "REDIRECT_URIS_REQUIRED")
    for value in values:
        p = urlsplit(string(value))
        require(
            not p.username and not p.password and not p.fragment and "*" not in value,
            "EXACT_REDIRECT_REQUIRED",
        )
        require(
            p.scheme == "https"
            or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1")),
            "SECURE_REDIRECT_REQUIRED",
        )
