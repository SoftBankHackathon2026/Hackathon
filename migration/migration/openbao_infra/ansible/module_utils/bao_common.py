"""Shared target-side primitives. Error codes and reports never contain secrets."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import stat
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


class BaoError(Exception):
    pass


def ensure(condition, code):
    if not condition:
        raise BaoError(code)


def read_private(path, binary=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb" if binary else "r") as stream:
        info = os.fstat(stream.fileno())
        ensure(
            stat.S_ISREG(info.st_mode)
            and not info.st_mode & 0o077
            and (info.st_uid == os.getuid() or (os.getuid() == 0 and binary)),
            "UNSAFE_FILE",
        )
        data = stream.read(1048577)
        ensure(len(data) <= 1048576, "FILE_TOO_LARGE")
        return data


def atomic(path, data, mode=0o600):
    p = Path(path)
    ensure(not p.is_symlink(), "UNSAFE_FILE")
    fd, temporary = tempfile.mkstemp(dir=p.parent, prefix=".bao-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data if isinstance(data, bytes) else data.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, p)
        directory = os.open(p.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    atomic(path, json.dumps(value, sort_keys=True))


def read_json(path):
    return json.loads(read_private(path))


def same(actual, desired):
    """Server responses may contain defaults; compare fields we own only."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            k in actual and same(actual[k], v) for k, v in desired.items()
        )
    if isinstance(desired, list):
        if not isinstance(actual, list) or len(actual) != len(desired):
            return False
        if any(isinstance(v, dict) for v in desired):
            return all(any(same(a, d) for a in actual) for d in desired)
        return sorted(actual) == sorted(desired)
    return actual == desired


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, endpoint, ca_file, token=None):
        u = urlsplit(endpoint)
        ensure(
            u.scheme == "https"
            and u.hostname
            and not u.username
            and not u.password
            and not u.query
            and not u.fragment
            and u.path in ("", "/"),
            "INVALID_ENDPOINT",
        )
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.opener = build_opener(
            NoRedirect(),
            HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
        )

    def call(self, method, path, body=None, allow=(200, 204)):
        headers = {"Content-Type": "application/json", "X-Vault-Request": "true"}
        if self.token:
            headers["X-Vault-Token"] = self.token
        request = Request(
            self.endpoint + "/v1/" + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                code = response.status
                data = response.read(1048577)
        except HTTPError as exc:
            code = exc.code
            if code not in allow:
                raise BaoError(
                    {
                        403: "ACCESS_DENIED",
                        404: "NOT_FOUND",
                        409: "CONFLICT",
                        503: "SEALED",
                    }.get(code, f"HTTP_STATUS_{code}")
                ) from None
            data = exc.read(1048577)
        except (URLError, OSError, ssl.SSLError):
            raise BaoError("CONNECTION_OR_TLS_FAILED") from None
        ensure(code in allow and len(data) <= 1048576, "INVALID_RESPONSE")
        try:
            return json.loads(data) if data else {}
        except (ValueError, UnicodeError):
            raise BaoError("INVALID_RESPONSE") from None

    def health(self):
        return self.call("GET", "sys/health", allow=(200, 429, 472, 473, 501, 503))

    def reconcile(self, path, body, *, read_path=None, field=None):
        try:
            actual = self.call("GET", read_path or path).get("data", {})
        except BaoError as exc:
            if str(exc) != "NOT_FOUND":
                raise
            actual = None
        if field and actual is not None:
            actual = actual.get(field)
        # Policy reads use a text field; config reads may intentionally omit secrets.
        if same(actual, body):
            return False
        self.call("POST", path, body)
        return True


def bundle(key_path, bootstrap_path, installation_id, cluster_id):
    key = read_private(key_path, binary=True)
    ensure(len(key) == 32, "STATIC_KEY_INVALID")
    bootstrap = read_json(bootstrap_path)
    return {
        "schema_version": 1,
        "installation_id": installation_id,
        "cluster_id": cluster_id,
        "static_key_base64": base64.b64encode(key).decode(),
        "recovery_keys_base64": bootstrap.get("recovery_keys_base64", []),
        "warning": "Keep offline. Static key and recovery keys have different purposes. No root token included.",
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
