from __future__ import annotations

import math
from urllib.parse import quote

from migration.secrets.common import SecretsError, canonical, logical_path, require
from migration.secrets.models import SecretValue
from migration.secrets.target import MAX_TOKEN_TTL_SECONDS, Http

SELECTION_LIMIT = 1000
VALUE_LIMIT = 65536
READ_CAPABILITIES = frozenset({"read", "list"})
ROW_BLOCKING_CODES = frozenset(
    {
        "SOURCE_NOT_FOUND",
        "SOURCE_VERSION_DELETED",
        "SOURCE_VERSION_UNREADABLE",
        "SOURCE_VALUE_TOO_LARGE",
    }
)


class OpenBaoSource:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or Http(
            config["endpoint"], config["ca_file"], config["token_ref"]
        )

    def __repr__(self):
        return "<OpenBaoSource [REDACTED]>"

    def request(self, method, path, payload=None):
        try:
            return self.http.request(method, path, payload)
        except SecretsError as error:
            code = error.code
            if code.startswith("TARGET_"):
                code = "SOURCE_" + code[len("TARGET_") :]
            raise SecretsError(code) from None

    def data_path(self, path):
        logical_path(path)
        return self.config["mount"] + "/data/" + quote(path, safe="/")

    def metadata_path(self, path):
        logical_path(path)
        return self.config["mount"] + "/metadata/" + quote(path, safe="/")

    def identity(self):
        health = self.request("GET", "sys/health?standbyok=true")
        require(
            health.get("initialized") is True and health.get("sealed") is False,
            "OPENBAO_NOT_READY",
        )
        require(
            health.get("cluster_id") == self.config["cluster_id"]
            and health.get("version") == self.config["expected_version"],
            "SOURCE_IDENTITY_MISMATCH",
        )
        mount = self.request("GET", "sys/internal/ui/mounts/" + self.config["mount"])
        mount = mount.get("data") if isinstance(mount, dict) else None
        require(
            isinstance(mount, dict)
            and mount.get("type") == "kv"
            and str((mount.get("options") or {}).get("version")) == "2"
            and mount.get("path") == self.config["mount"] + "/"
            and bool(mount.get("accessor")),
            "KV_V2_REQUIRED",
        )
        token = self.request("GET", "auth/token/lookup-self").get("data") or {}
        ttl = token.get("ttl")
        require(
            "root" not in (token.get("policies") or [])
            and isinstance(ttl, (int, float))
            and not isinstance(ttl, bool)
            and math.isfinite(ttl)
            and 0 < ttl <= MAX_TOKEN_TTL_SECONDS,
            "FINITE_NONROOT_TOKEN_REQUIRED",
        )
        return {
            "cluster_id": health["cluster_id"],
            "accessor": mount["accessor"],
            "mount": self.config["mount"],
            "version": health["version"],
        }

    def check_read_only(self, paths):
        data = [self.data_path(p) for p in paths]
        metadata = [self.metadata_path(p) for p in paths]
        denied = [
            p.replace("/data/", "/" + action + "/", 1)
            for p in data
            for action in ("delete", "destroy", "undelete")
        ]
        denied.extend(["sys/auth", "sys/policies/acl/unrelated"])
        response = self.request(
            "POST", "sys/capabilities-self", {"paths": [*data, *metadata, *denied]}
        )
        caps = response.get("data", response)
        require(isinstance(caps, dict), "SOURCE_RESPONSE_INVALID")
        for path in data:
            granted = caps.get(path)
            require(
                isinstance(granted, list) and "read" in granted,
                "SOURCE_PERMISSION_MISSING",
            )
            require(set(granted) <= READ_CAPABILITIES, "SOURCE_TOKEN_NOT_READ_ONLY")
        require(
            all(
                isinstance(caps.get(p), list)
                and set(caps[p]) <= READ_CAPABILITIES | {"deny"}
                for p in metadata
            )
            and all(caps.get(p) == ["deny"] for p in denied),
            "SOURCE_TOKEN_NOT_READ_ONLY",
        )

    def discover(self):
        found = set(self.config.get("paths", []))
        blockers = []
        for prefix in self.config.get("prefixes", []):
            pending = [prefix]
            while pending:
                folder = pending.pop()
                try:
                    listed = self.request(
                        "GET", self.metadata_path(folder) + "?list=true"
                    )
                except SecretsError as error:
                    if error.code != "SOURCE_NOT_FOUND":
                        raise
                    if folder == prefix:
                        blockers.append(
                            {"prefix": prefix, "code": "SOURCE_PREFIX_EMPTY"}
                        )
                    continue
                names = (listed.get("data") or {}).get("keys")
                require(isinstance(names, list), "SOURCE_RESPONSE_INVALID")
                for name in names:
                    require(
                        isinstance(name, str) and name.strip("/"),
                        "SOURCE_RESPONSE_INVALID",
                    )
                    child = logical_path(folder + "/" + name.rstrip("/"))
                    if name.endswith("/"):
                        pending.append(child)
                    else:
                        found.add(child)
                    require(
                        len(found) + len(pending) <= SELECTION_LIMIT,
                        "SOURCE_SELECTION_TOO_LARGE",
                    )
        return sorted(found), blockers

    def read(self, path):
        raw = self.request("GET", self.data_path(path))
        body = raw.get("data") if isinstance(raw, dict) else None
        require(
            isinstance(body, dict)
            and isinstance(body.get("metadata"), dict)
            and isinstance(body.get("data"), dict),
            "SOURCE_VERSION_UNREADABLE",
        )
        meta = body["metadata"]
        version = meta.get("version")
        require(
            type(version) is int
            and version > 0
            and not meta.get("destroyed")
            and not meta.get("deletion_time"),
            "SOURCE_VERSION_UNREADABLE",
        )
        data = canonical(body["data"]).encode("utf-8")
        require(len(data) <= VALUE_LIMIT, "SOURCE_VALUE_TOO_LARGE")
        return SecretValue(data, "json"), version
