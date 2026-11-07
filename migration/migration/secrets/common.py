"""Strict, private artifacts and fixed local errors; never serialize remote exceptions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import yaml


class SecretsError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(test, code):
    if not test:
        raise SecretsError(code)


def keys(value, allowed, required=()):
    require(isinstance(value, dict), "OBJECT_REQUIRED")
    require(not set(value) - set(allowed), "UNKNOWN_FIELD")
    require(set(required) <= set(value), "MISSING_FIELD")


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_fingerprint(inventory):
    complete = inventory.get("status", "complete") == "complete" and all(
        c.get("status") == "collected" for c in inventory.get("coverage", [])
    )
    return digest(
        {
            "source": inventory["source"],
            "resources": inventory["resources"],
            "complete": complete,
        }
    )


def now():
    return datetime.now(UTC).isoformat()


def unique_pairs(items):
    result = {}
    for k, v in items:
        require(isinstance(k, str) and k not in result, "DUPLICATE_KEY")
        result[k] = v
    return result


def json_value(text):
    def reject(_):
        raise SecretsError("INVALID_JSON")

    try:
        return json.loads(text, object_pairs_hook=unique_pairs, parse_constant=reject)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise SecretsError("INVALID_JSON") from None


def read_file(path, *, private=False, limit=16 * 1024 * 1024):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode), "UNSAFE_FILE")
            if private:
                require(
                    info.st_uid == os.getuid() and not info.st_mode & 0o077,
                    "PRIVATE_FILE_REQUIRED",
                )
            data = stream.read(limit + 1)
        require(len(data) <= limit, "INPUT_TOO_LARGE")
        return data
    except OSError:
        raise SecretsError("INPUT_UNREADABLE") from None


def load(path, *, private=True):
    text = read_file(path, private=private).decode("utf-8")
    if text.lstrip().startswith(("{", "[")):
        return json_value(text)

    class Loader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        return unique_pairs(
            (loader.construct_object(k), loader.construct_object(v))
            for k, v in node.value
        )

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        return yaml.load(text, Loader=Loader)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise SecretsError("INVALID_YAML") from None


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not path.is_symlink(), "UNSAFE_OUTPUT")
    fd, tmp = tempfile.mkstemp(prefix=".secret-import-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(
                json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def string(value):
    require(
        isinstance(value, str)
        and 0 < len(value) <= 2048
        and not any(ord(c) < 32 for c in value),
        "INVALID_STRING",
    )
    return value


def logical_path(value):
    string(value)
    require(
        bool(re.fullmatch(r"[A-Za-z0-9_./-]+", value))
        and all(p not in {"", ".", ".."} for p in value.split("/")),
        "INVALID_PATH",
    )
    return value


def https_url(value):
    string(value)
    try:
        p = urlsplit(value)
        valid = (
            p.scheme == "https"
            and p.hostname
            and p.port != 0
            and not p.username
            and not p.password
            and not p.query
            and not p.fragment
            and p.path in {"", "/"}
        )
    except ValueError:
        valid = False
    require(
        valid and not any(c.isspace() for c in value) and "\\" not in value,
        "HTTPS_ENDPOINT_REQUIRED",
    )
    return value.rstrip("/")


def credential(ref):
    string(ref)
    if ref.startswith("env:"):
        name = ref[4:]
        require(
            bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)),
            "INVALID_CREDENTIAL_REF",
        )
        value = os.environ.get(name, "")
    elif ref.startswith("file:/"):
        value = read_file(ref[5:], private=True, limit=16384).decode().strip()
    else:
        raise SecretsError("INVALID_CREDENTIAL_REF")
    require(
        bool(value) and len(value) <= 16384 and all(33 <= ord(c) <= 126 for c in value),
        "CREDENTIAL_UNAVAILABLE",
    )
    return value


def seal_artifact(value, field):
    value[field] = digest({k: v for k, v in value.items() if k != field})
    return value


def check_artifact(value, kind, field):
    require(
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and type(value.get("schema_version")) is int
        and value.get("kind") == kind,
        "ARTIFACT_TYPE",
    )
    require(
        value.get(field) == digest({k: v for k, v in value.items() if k != field}),
        "ARTIFACT_CHANGED",
    )


def validate_config(c):
    keys(
        c,
        {
            "schema_version",
            "migration_id",
            "source",
            "target",
            "preparation",
            "identity",
        },
        {"schema_version", "migration_id", "source", "target", "preparation"},
    )
    require(
        type(c["schema_version"]) is int and c["schema_version"] == 1, "CONFIG_VERSION"
    )
    require(
        bool(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", string(c["migration_id"]))),
        "MIGRATION_ID",
    )
    s = c["source"]
    keys(
        s,
        {"account_id", "region", "profile", "include_names", "exclude_names"},
        {"account_id", "region", "include_names"},
    )
    require(bool(re.fullmatch(r"\d{12}", string(s["account_id"]))), "ACCOUNT_ID")
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{2,40}", string(s["region"]))), "REGION")
    for field in ("include_names", "exclude_names"):
        values = s.get(field, [])
        require(
            isinstance(values, list) and (values or field == "exclude_names"),
            "EXPLICIT_SELECTION_REQUIRED",
        )
        for value in values:
            string(value)
    if "profile" in s:
        string(s["profile"])
    t = c["target"]
    keys(
        t,
        {
            "endpoint",
            "expected_version",
            "cluster_id",
            "mount",
            "prefix",
            "ca_file",
            "token_ref",
        },
        {
            "endpoint",
            "expected_version",
            "cluster_id",
            "mount",
            "prefix",
            "ca_file",
            "token_ref",
        },
    )
    https_url(t["endpoint"])
    require(
        bool(re.fullmatch(r"2\.7\.\d+", string(t["expected_version"]))),
        "OPENBAO_VERSION",
    )
    string(t["cluster_id"])
    for f in ("mount", "prefix"):
        logical_path(t[f])
    require(
        "/" not in t["mount"]
        and t["mount"] not in {"sys", "auth", "identity", "cubbyhole"},
        "DEDICATED_KV_MOUNT",
    )
    for key in ("ca_file",):
        require(Path(string(t[key])).is_absolute(), "ABSOLUTE_FILE_REQUIRED")
    ref = string(t["token_ref"])
    require(
        ref.startswith("file:/") or re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*", ref),
        "INVALID_CREDENTIAL_REF",
    )
    keys(
        c["preparation"],
        {"configure_result", "verify_result"},
        {"configure_result", "verify_result"},
    )
    if c.get("identity") is not None:
        keys(
            c["identity"],
            {"config", "inventory", "plan"},
            {"config", "inventory", "plan"},
        )
    for paths in (c["preparation"], c.get("identity") or {}):
        for p in paths.values():
            require(Path(string(p)).is_absolute(), "ABSOLUTE_FILE_REQUIRED")
    return c
