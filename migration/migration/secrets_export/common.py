from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path

from migration.secrets.common import (
    credential,
    https_url,
    keys,
    logical_path,
    require,
    string,
)

RESERVED_MOUNTS = frozenset({"sys", "auth", "identity", "cubbyhole"})
NAME_PATTERN = re.compile(r"[A-Za-z0-9/_+=.@-]{1,512}")
KMS_PATTERN = re.compile(r"[A-Za-z0-9:/_-]{1,2048}")
FILE_REF = re.compile(r"file:/[^\s]+")
PATH_LIMIT = 256
HASH_KEY_MIN = 32
HASH_DOMAIN = b"secrets-export-v1\0"
TAG_PREFIX = "secrets-export:"
BINDING_TAG = TAG_PREFIX + "binding"


def file_ref(value):
    string(value)
    require(bool(FILE_REF.fullmatch(value)), "FILE_CREDENTIAL_REF_REQUIRED")
    require(Path(value[5:]).is_absolute(), "ABSOLUTE_FILE_REQUIRED")
    return value


def render_name(template, path):
    name = template.replace("{path}", path)
    require(bool(NAME_PATTERN.fullmatch(name)), "TARGET_NAME_INVALID")
    return name


def source_path(value):
    logical_path(value)
    require(len(value) <= PATH_LIMIT, "SOURCE_PATH_TOO_LONG")
    return value


def validate_config(c):
    fields = {"schema_version", "migration_id", "source", "target", "hash_key_ref"}
    keys(c, fields, fields)
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
        {
            "endpoint",
            "expected_version",
            "cluster_id",
            "mount",
            "paths",
            "prefixes",
            "ca_file",
            "token_ref",
        },
        {"endpoint", "expected_version", "cluster_id", "mount", "ca_file", "token_ref"},
    )
    https_url(s["endpoint"])
    require(
        bool(re.fullmatch(r"2\.7\.\d+", string(s["expected_version"]))),
        "OPENBAO_VERSION",
    )
    string(s["cluster_id"])
    logical_path(s["mount"])
    require(
        "/" not in s["mount"] and s["mount"] not in RESERVED_MOUNTS,
        "DEDICATED_KV_MOUNT",
    )
    selected = []
    for field in ("paths", "prefixes"):
        values = s.get(field, [])
        require(isinstance(values, list), "EXPLICIT_SELECTION_REQUIRED")
        selected.extend((field, source_path(v)) for v in values)
    require(
        bool(selected) and len(set(selected)) == len(selected),
        "EXPLICIT_SELECTION_REQUIRED",
    )
    require(Path(string(s["ca_file"])).is_absolute(), "ABSOLUTE_FILE_REQUIRED")
    file_ref(s["token_ref"])
    t = c["target"]
    keys(
        t,
        {
            "account_id",
            "region",
            "profile",
            "name_template",
            "kms_key_id",
            "adopt_existing",
        },
        {"account_id", "region", "name_template"},
    )
    require(bool(re.fullmatch(r"\d{12}", string(t["account_id"]))), "ACCOUNT_ID")
    require(bool(re.fullmatch(r"[a-z][a-z0-9-]{2,40}", string(t["region"]))), "REGION")
    if "profile" in t:
        string(t["profile"])
    template = string(t["name_template"])
    require(
        template.count("{path}") == 1
        and not set("{}") & set(template.replace("{path}", "")),
        "NAME_TEMPLATE_INVALID",
    )
    render_name(template, "x")
    if t.get("kms_key_id") is not None:
        require(
            bool(KMS_PATTERN.fullmatch(string(t["kms_key_id"]))), "KMS_KEY_ID_INVALID"
        )
    require(type(t.get("adopt_existing", False)) is bool, "ADOPT_EXISTING_INVALID")
    file_ref(c["hash_key_ref"])
    require(
        Path(c["hash_key_ref"][5:]).resolve() != Path(s["token_ref"][5:]).resolve(),
        "HASH_KEY_MUST_DIFFER_FROM_TOKEN",
    )
    return c


def hash_key(config):
    key = credential(config["hash_key_ref"]).encode()
    require(len(key) >= HASH_KEY_MIN, "HASH_KEY_TOO_SHORT")
    return key


def value_hash(key, value):
    kind = b"binary" if value.kind == "binary" else b"string"
    return hmac.new(
        key, HASH_DOMAIN + kind + b"\0" + value.data, hashlib.sha256
    ).hexdigest()


def key_check(key):
    return hmac.new(key, HASH_DOMAIN + b"key-check", hashlib.sha256).hexdigest()


def same_hash(left, right):
    return (
        isinstance(left, str)
        and isinstance(right, str)
        and hmac.compare_digest(left, right)
    )
