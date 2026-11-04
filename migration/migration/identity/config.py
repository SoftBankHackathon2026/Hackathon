"""Bounded, duplicate-safe file input; credentials are never config values."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
import yaml
from .models import ConfigError, MigrationSpec, primitive, reject_secrets


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for k, v in node.value:
        key = loader.construct_object(k, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigError("duplicate or non-string input key")
        result[key] = loader.construct_object(v, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def read(path: str | Path) -> dict:
    try:
        data = Path(path).read_bytes()
        if len(data) > 16 * 1024 * 1024:
            raise ConfigError("artifact exceeds 16 MiB")
        raw = yaml.load(data.decode("utf-8"), Loader=UniqueLoader)
        reject_secrets(raw)
        if not isinstance(raw, dict):
            raise ConfigError("input requires a mapping")
        return raw
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError, ValueError, TypeError) as exc:
        raise ConfigError("cannot read artifact safely") from None


def load_spec(path: str | Path) -> MigrationSpec:
    return MigrationSpec.from_dict(read(path))


def write(path: str | Path, value: object) -> None:
    value = primitive(value)
    reject_secrets(value)
    data = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=destination.parent, prefix=".identity-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, destination)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
