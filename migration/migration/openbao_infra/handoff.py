"""Private local download artifacts. HTTP delivery/authentication belongs to platform."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigError, require, secret_file


def private_json(path, value):
    p = Path(path)
    require(not p.is_symlink(), "output.symlink")
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".bao-")
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def binding(c):
    t = c["target"]
    node = next(n for n in c["nodes"] if n["name"] == t["node_name"])
    return {
        "cluster_id": c["cluster"]["id"],
        "installation_id": t["installation_id"],
        "node_uid": node["uid"],
        "endpoint": c["endpoint"]["url"],
        "owner_id": c["handoff"]["owner_id"],
    }


@contextmanager
def locked(c):
    directory = Path(c["handoff"]["directory"])
    require(directory.is_dir() and not directory.is_symlink(), "handoff.directory")
    info = directory.stat()
    require(
        info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "handoff.directory.permissions",
    )
    receipt = Path(c["handoff"]["receipt_path"])
    require(receipt.parent.resolve() == directory.resolve(), "handoff.receipt_path")
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory, receipt
    finally:
        os.close(fd)


def _read(receipt, c):
    state = json.loads(secret_file(str(receipt)))
    require(state.get("binding") == binding(c), "handoff.binding")
    require(
        re.fullmatch(r"[a-f0-9]{32}\.bundle\.json", state.get("artifact", "")),
        "handoff.artifact",
    )
    return state


def prepare(c, source, now=None):
    now = time.time() if now is None else now
    raw = secret_file(str(source))
    data = json.loads(raw)
    require(
        data.get("installation_id") == c["target"]["installation_id"],
        "handoff.installation_id",
    )
    require("root_token" not in data, "handoff.root_token")
    with locked(c) as (directory, receipt):
        if receipt.exists():
            old = _read(receipt, c)
            require(old["status"] in ("expired", "cleaned"), "handoff.already_prepared")
        name = uuid.uuid4().hex + ".bundle.json"
        private_json(directory / name, data)
        state = {
            "schema_version": 1,
            "binding": binding(c),
            "artifact": name,
            "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            "expires_at": now + c["handoff"]["ttl_seconds"],
            "status": "prepared",
        }
        try:
            private_json(receipt, state)
        except BaseException:
            (directory / name).unlink(missing_ok=True)
            raise
        return {
            "status": "handoff_prepared",
            "artifact_ref": str(directory / name),
            "receipt_ref": str(receipt),
            "expires_at": state["expires_at"],
            "sha256": state["sha256"],
        }


def complete(c, now=None):
    now = time.time() if now is None else now
    with locked(c) as (directory, receipt):
        state = _read(receipt, c)
        action = c["handoff"]["action"]
        if action == "ack":
            require(
                c["handoff"].get("download_confirmed") is True,
                "handoff.download_confirmed",
            )
            if state["status"] == "acknowledged":
                (directory / state["artifact"]).unlink(missing_ok=True)
                return {"status": "handoff_acknowledged", "receipt_ref": str(receipt)}
            require(
                state["status"] == "prepared" and now < state["expires_at"],
                "handoff.expired_or_cleaned",
            )
            artifact = directory / state["artifact"]
            require(
                not artifact.is_symlink() and artifact.is_file(), "handoff.artifact"
            )
            require(
                hashlib.sha256(artifact.read_bytes()).hexdigest() == state["sha256"],
                "handoff.digest",
            )
            # Commit authorization revocation first; platform must consult receipt before streaming.
            state["status"] = "acknowledged"
            private_json(receipt, state)
            artifact.unlink()
            return {"status": "handoff_acknowledged", "receipt_ref": str(receipt)}
        require(action == "cleanup", "handoff.action")
        if state["status"] == "acknowledged":
            (directory / state["artifact"]).unlink(missing_ok=True)
        elif now >= state["expires_at"]:
            state["status"] = "expired"
            private_json(receipt, state)
            (directory / state["artifact"]).unlink(missing_ok=True)
        else:
            raise ConfigError("handoff artifact is still active")
        return {
            "status": "handoff_cleaned",
            "receipt_ref": str(receipt),
            "download_acknowledged": state["status"] == "acknowledged",
        }
