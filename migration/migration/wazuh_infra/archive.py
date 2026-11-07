"""Validate an external original-transfer manifest; never downloads source logs."""

import re
from datetime import datetime
from pathlib import PurePosixPath


def validate_manifest(record):
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
    ):
        raise ValueError("ARCHIVE_MANIFEST_INVALID")
    required = (
        "source_id",
        "object_key",
        "local_relative_path",
        "bytes",
        "sha256",
        "archived_at",
        "verification",
    )
    if any(k not in record for k in required):
        raise ValueError("ARCHIVE_MANIFEST_INVALID")
    if (
        set(record)
        - set(required)
        - {"schema_version", "source_version", "archive_fidelity"}
    ):
        raise ValueError("ARCHIVE_MANIFEST_INVALID")
    if not isinstance(record["source_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", record["source_id"]
    ):
        raise ValueError("ARCHIVE_SOURCE_INVALID")
    if not isinstance(record["object_key"], str) or not record["object_key"]:
        raise ValueError("ARCHIVE_SOURCE_INVALID")
    try:
        timestamp = datetime.fromisoformat(record["archived_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("ARCHIVE_TIME_INVALID") from None
    path = record["local_relative_path"]
    if (
        not isinstance(path, str)
        or not path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or "\x00" in path
        or "\\" in path
        or str(PurePosixPath(path)) != path
    ):
        raise ValueError("ARCHIVE_PATH_INVALID")
    if type(record["bytes"]) is not int or record["bytes"] < 0:
        raise ValueError("ARCHIVE_SIZE_INVALID")
    digest = record["sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ValueError("ARCHIVE_CHECKSUM_INVALID")
    if record["verification"] not in ("local_only", "remote_compared"):
        raise ValueError("ARCHIVE_VERIFICATION_INVALID")
    if record.get("archive_fidelity", "source_file") not in (
        "source_file",
        "api_records",
    ):
        raise ValueError("ARCHIVE_FIDELITY_INVALID")
    if record.get("source_version") is not None and not isinstance(
        record["source_version"], str
    ):
        raise ValueError("ARCHIVE_VERSION_INVALID")
    return {
        "schema_version": 1,
        "source_id": record["source_id"],
        "bytes": record["bytes"],
        "verification": record["verification"],
        "search_verified": False,
    }
