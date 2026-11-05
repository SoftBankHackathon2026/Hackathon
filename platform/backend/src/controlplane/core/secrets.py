"""Read operator-managed secret files at startup; never used on HTTP request input."""

import os
import stat
from pathlib import Path

from pydantic import Field, SecretStr, model_validator

from controlplane.domain.models import Model

MAX_SECRET_BYTES = 65536


def read_secret_file(path: str | Path) -> SecretStr:
    """Allow owner/group read, including projected-volume symlinks, but no public access."""
    try:
        path = Path(path)
        if not path.is_absolute():
            raise ValueError
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o027:
                raise ValueError
            value = stream.read(MAX_SECRET_BYTES + 1)
        if len(value) > MAX_SECRET_BYTES:
            raise ValueError
        text = value.decode("utf-8").rstrip("\r\n")
        if not text.strip() or any(c in text for c in ("\n", "\r", "\x00")):
            raise ValueError
        return SecretStr(text)
    except (OSError, ValueError, TypeError):
        raise ValueError(
            "Invalid secret file: use an absolute, readable, nonempty single-line file "
            "with no group write or public permissions (for example 0400 or 0640)"
        ) from None


def secret_from_environment(name: str) -> SecretStr:
    value = os.getenv(name)
    path = os.getenv(name + "_FILE")
    if value is not None and path is not None:
        raise ValueError(f"Set only {name} or {name}_FILE")
    if path is not None:
        return read_secret_file(path)
    return SecretStr(value or "")


class DatabaseSettings(Model):
    """All API/worker configurations resolve the same DB secret before validation."""

    database_url: SecretStr
    database_url_file: Path | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="before")
    @classmethod
    def resolve_database_file(cls, value):
        if isinstance(value, dict) and value.get("database_url_file") is not None:
            if "database_url" in value:
                raise ValueError("Set only database_url or database_url_file")
            value = dict(value)
            value["database_url"] = read_secret_file(value["database_url_file"]).get_secret_value()
        return value
