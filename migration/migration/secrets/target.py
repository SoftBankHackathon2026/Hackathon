"""Scoped KV v2 documents: original bytes plus explicit current/previous references.

Only data create/update/read is needed. No metadata writes, root token, policy
management or sys/mounts read is required from the migration token.
"""

from __future__ import annotations

import base64
import hmac
import math
import ssl
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .common import (
    SecretsError,
    canonical,
    credential,
    https_url,
    json_value,
    logical_path,
    require,
)
from .models import SecretValue

SCHEMA = "aws-openbao-secret-v1"
MAX_TOKEN_TTL_SECONDS = 24 * 60 * 60


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self, endpoint, ca_file, token_ref):
        self.endpoint = https_url(endpoint)
        self.ref = token_ref
        self.token = credential(token_ref)
        self.opener = build_opener(
            NoRedirect(),
            HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
        )

    def __repr__(self):
        return "<OpenBaoHttp [REDACTED]>"

    def request(self, method, path, payload=None):
        require(
            hmac.compare_digest(self.token, credential(self.ref)),
            "CREDENTIAL_CHANGED_RESTART_REQUIRED",
        )
        data = canonical(payload).encode() if payload is not None else None
        require(data is None or len(data) <= 2 * 1024 * 1024, "DOCUMENT_TOO_LARGE")
        try:
            req = Request(
                self.endpoint + "/v1/" + path,
                data=data,
                method=method,
                headers={
                    "X-Vault-Token": self.token,
                    "Content-Type": "application/json",
                },
            )
            with self.opener.open(req, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                require(len(raw) <= 2 * 1024 * 1024, "RESPONSE_TOO_LARGE")
                return json_value(raw) if raw else {}
        except HTTPError as e:
            if e.code == 404 and deleted_version(e):
                raise SecretsError("TARGET_VERSION_DELETED") from None
            raise SecretsError(
                {
                    403: "TARGET_ACCESS_DENIED",
                    404: "TARGET_NOT_FOUND",
                    400: "TARGET_WRITE_CONFLICT",
                }.get(e.code, "TARGET_UNAVAILABLE")
            ) from None
        except (OSError, ValueError):
            raise SecretsError("TARGET_UNAVAILABLE") from None


def deleted_version(error):
    try:
        body = json_value(error.read(64 * 1024))
        metadata = body["data"]["metadata"]
    except Exception:
        return False
    return isinstance(metadata, dict) and bool(
        metadata.get("deletion_time") or metadata.get("destroyed")
    )


def encode(value):
    return {"kind": value.kind, "payload": base64.b64encode(value.data).decode("ascii")}


def decode(value):
    try:
        require(
            set(value) == {"kind", "payload"} and value["kind"] in {"string", "binary"},
            "VALUE_FORMAT_INVALID",
        )
        return SecretValue(
            base64.b64decode(value["payload"], validate=True), value["kind"]
        )
    except (ValueError, TypeError):
        raise SecretsError("VALUE_FORMAT_INVALID") from None


class OpenBao:
    def __init__(self, config, http=None):
        self.config = config
        self.http = http or Http(
            config["endpoint"], config["ca_file"], config["token_ref"]
        )

    def path(self, name):
        logical_path(name)
        require(name.startswith(self.config["prefix"] + "/"), "TARGET_SCOPE_MISMATCH")
        return self.config["mount"] + "/data/" + quote(name, safe="/")

    def inspect(self, paths):
        h = self.http.request("GET", "sys/health?standbyok=true")
        require(
            h.get("initialized") is True and h.get("sealed") is False,
            "OPENBAO_NOT_READY",
        )
        require(
            h.get("cluster_id") == self.config["cluster_id"]
            and h.get("version") == self.config["expected_version"],
            "TARGET_IDENTITY_MISMATCH",
        )
        # This endpoint checks access to the requested path. It does not require
        # global sys/mounts privileges and exposes the actual mount accessor.
        mount = self.http.request(
            "GET", "sys/internal/ui/mounts/" + self.path(paths[0])
        )["data"]
        require(
            mount.get("type") == "kv"
            and str(mount.get("options", {}).get("version")) == "2"
            and mount.get("path") == self.config["mount"] + "/"
            and mount.get("accessor"),
            "KV_V2_REQUIRED",
        )
        token = self.http.request("GET", "auth/token/lookup-self")["data"]
        ttl = token.get("ttl")
        require(
            "root" not in token.get("policies", [])
            and isinstance(ttl, (int, float))
            and not isinstance(ttl, bool)
            and math.isfinite(ttl)
            and 0 < ttl <= MAX_TOKEN_TTL_SECONDS,
            "FINITE_NONROOT_TOKEN_REQUIRED",
        )
        required = {self.path(p): ["create", "update", "read"] for p in paths}
        outside = self.config["mount"] + "/data/__outside_secret_migration__/probe"
        # A prefix equal to the reserved denial probe cannot enter via config.
        require(
            not outside.startswith(
                self.config["mount"] + "/data/" + self.config["prefix"] + "/"
            ),
            "INVALID_PREFIX",
        )
        sibling = (
            self.config["mount"]
            + "/data/"
            + self.config["prefix"]
            + "__outside_secret_migration__/probe"
        )
        lifecycle = [
            path.replace("/data/", "/" + action + "/", 1)
            for path in required
            for action in ("metadata", "delete", "destroy", "undelete")
        ]
        denied = [
            outside,
            sibling,
            *lifecycle,
            "sys/auth",
            "sys/policies/acl/unrelated",
        ]
        requested = [*required, *denied]
        response = self.http.request(
            "POST", "sys/capabilities-self", {"paths": requested}
        )
        caps = response.get("data", response)
        for path, wanted in required.items():
            require(
                set(wanted) == set(caps.get(path, [])),
                "MIGRATION_PERMISSION_MISSING",
            )
        require(
            all(caps.get(p) == ["deny"] for p in denied),
            "MIGRATION_TOKEN_OVERPRIVILEGED",
        )
        return {
            "cluster_id": h["cluster_id"],
            "accessor": mount["accessor"],
            "mount": self.config["mount"],
            "version": h["version"],
        }

    def read_document(self, path):
        try:
            raw = self.http.request("GET", self.path(path))["data"]
        except SecretsError as error:
            if error.code == "TARGET_NOT_FOUND":
                return None
            raise
        version = raw["metadata"]["version"]
        require(
            type(version) is int
            and version > 0
            and not raw["metadata"].get("destroyed")
            and not raw["metadata"].get("deletion_time"),
            "TARGET_VERSION_UNREADABLE",
        )
        return raw["data"], version

    def write_document(self, path, document, expected_version):
        result = self.http.request(
            "POST",
            self.path(path),
            {"options": {"cas": expected_version}, "data": document},
        )
        require(
            type(result.get("data", {}).get("version")) is int
            and result["data"]["version"] == expected_version + 1,
            "TARGET_WRITE_RESPONSE_INVALID",
        )
        return result["data"]["version"]

    def read_value(self, path, reference="current"):
        result = self.read_document(path)
        require(result is not None, "TARGET_NOT_FOUND")
        document, _ = result
        require(
            document.get("schema") == SCHEMA
            and reference in {"current", "previous", "pending"},
            "VALUE_FORMAT_INVALID",
        )
        require(
            document.get(reference) in document.get("values", {}),
            "REFERENCE_UNAVAILABLE",
        )
        return decode(document["values"][document[reference]])
