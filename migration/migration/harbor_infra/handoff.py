"""Operational credentials persist; only short-lived export copies are cleaned."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .ansible.module_utils.harbor_core import check, locked, read, save


def binding(c):
    return {
        "organization_id": c["organization_id"],
        "installation_id": c["target"]["installation_id"],
        "endpoint": c["endpoint"]["url"],
    }


def state_directory(c):
    return (
        Path(c["handoff"]["directory"])
        / c["organization_id"]
        / c["target"]["installation_id"]
    )


def execute(c):
    with locked(state_directory(c)) as root:
        export, record = root / "export.json", root / "export-record.json"
        action = c["handoff"]["action"]
        metadata = read(record)
        if action == "prepare":
            source = read(root / "credentials.json")
            check(source and source["binding"] == binding(c), "CREDENTIALS_NOT_READY")
            check(
                bool(source["accounts"])
                and all(
                    not v.get("pending") and v.get("expires_at", 0) > time.time()
                    for v in source["accounts"].values()
                ),
                "CREDENTIALS_INCOMPLETE_OR_EXPIRED",
            )
            if metadata:
                check(metadata["binding"] == binding(c), "STATE_TARGET_MISMATCH")
                if metadata.get("acknowledged"):
                    return {"status": "handoff_acknowledged", "reason_codes": []}
                if export.exists() and metadata["expires_at"] > time.time():
                    check(
                        hashlib.sha256(export.read_bytes()).hexdigest()
                        == metadata["sha256"],
                        "HANDOFF_ARTIFACT_CHANGED",
                    )
                    return {
                        "status": "handoff_prepared",
                        "artifact_ref": str(export),
                        **metadata,
                        "reason_codes": [],
                    }
            expires = int(time.time()) + c["handoff"]["ttl_seconds"]
            save(export, {**source, "expires_at": expires})
            metadata = {
                "binding": binding(c),
                "expires_at": expires,
                "sha256": hashlib.sha256(export.read_bytes()).hexdigest(),
                "acknowledged": False,
            }
            save(record, metadata)
            return {
                "status": "handoff_prepared",
                "artifact_ref": str(export),
                **metadata,
                "reason_codes": [],
            }
        # Cleanup must remain possible even if operational credentials expired or disappeared.
        check(metadata and metadata["binding"] == binding(c), "HANDOFF_RECORD_MISSING")
        if action == "ack":
            check(
                metadata["sha256"] == c["handoff"]["receipt_digest"],
                "HANDOFF_RECEIPT_INVALID_OR_EXPIRED",
            )
            if not metadata.get("acknowledged"):
                check(
                    metadata["expires_at"] > time.time()
                    and export.is_file()
                    and hashlib.sha256(export.read_bytes()).hexdigest()
                    == metadata["sha256"],
                    "HANDOFF_RECEIPT_INVALID_OR_EXPIRED",
                )
            metadata["acknowledged"] = True
            save(record, metadata)
            if export.exists():
                export.unlink()
            return {"status": "handoff_acknowledged", "reason_codes": []}
        check(
            metadata["acknowledged"] or metadata["expires_at"] <= time.time(),
            "HANDOFF_NOT_ACKNOWLEDGED_OR_EXPIRED",
        )
        if export.exists():
            export.unlink()
        return {"status": "handoff_cleaned", "reason_codes": []}
