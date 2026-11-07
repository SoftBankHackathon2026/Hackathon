from __future__ import annotations

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REPOSITORY = re.compile(
    r"[a-z0-9]+(?:(?:[.]|__?|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[.]|__?|-+)[a-z0-9]+)*)*"
)
TAG = re.compile(r"[\w][\w.-]{0,127}", re.ASCII)
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,254}")
HOST = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


class MigrationError(Exception):
    """A fixed reason code safe for reports."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise MigrationError(code)


def matches(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def object_keys(
    value: object, required: set[str], optional: set[str] = frozenset()
) -> dict:
    require(isinstance(value, dict), "INVALID_CONFIG")
    require(required <= value.keys() <= required | optional, "INVALID_CONFIG")
    return value


def absolute_path(value: object) -> Path:
    require(isinstance(value, str) and "\x00" not in value, "INVALID_CONFIG")
    path = Path(value)
    require(path.is_absolute(), "INVALID_CONFIG")
    return path


@dataclass(frozen=True)
class Config:
    request_id: str
    account: str
    region: str
    profile: str | None
    endpoint: str
    organization: str
    installation: str
    project: str
    overrides: dict[str, str]
    credentials_ref: Path
    ca_ref: Path | None
    exclusive_write: bool
    skopeo_version: str
    copy_timeout: int
    state_dir: Path

    @property
    def source_host(self) -> str:
        suffix = (
            "amazonaws.com.cn" if self.region.startswith("cn-") else "amazonaws.com"
        )
        return f"{self.account}.dkr.ecr.{self.region}.{suffix}"

    @property
    def target_host(self) -> str:
        return urlsplit(self.endpoint).netloc

    @property
    def binding(self) -> dict:
        return {
            "request_id": self.request_id,
            "account": self.account,
            "region": self.region,
            "endpoint": self.endpoint,
            "organization_id": self.organization,
            "installation_id": self.installation,
            "default_project": self.project,
            "project_overrides": self.overrides,
        }

    def target(self, repo: str) -> str:
        target = f"{self.overrides.get(repo, self.project)}/{repo}"
        require(
            len(target) <= 255 and matches(REPOSITORY, target),
            "INVALID_TARGET_REPOSITORY",
        )
        return target


def load_config(path: Path) -> Config:
    try:
        doc = object_keys(
            yaml.safe_load(path.read_text()),
            {"schema_version", "request_id", "source", "target", "execution"},
        )
        source = object_keys(doc["source"], {"account_id", "region"}, {"profile"})
        target = object_keys(
            doc["target"],
            {"url", "organization_id", "installation_id", "project", "credentials_ref"},
            {"ca_ref", "project_overrides"},
        )
        run = object_keys(
            doc["execution"],
            {"state_dir", "skopeo_version"},
            {"exclusive_write", "copy_timeout_seconds"},
        )
        require(
            type(doc["schema_version"]) is int and doc["schema_version"] == 1,
            "INVALID_CONFIG",
        )
        for value in (
            doc["request_id"],
            target["organization_id"],
            target["installation_id"],
        ):
            require(matches(IDENTIFIER, value), "INVALID_CONFIG")
        require(
            matches(re.compile(r"[0-9]{12}"), source["account_id"]), "INVALID_CONFIG"
        )
        require(
            matches(re.compile(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+"), source["region"]),
            "INVALID_CONFIG",
        )
        profile = source.get("profile")
        require(profile is None or matches(IDENTIFIER, profile), "INVALID_CONFIG")
        require(isinstance(target["url"], str), "INVALID_CONFIG")
        url = urlsplit(target["url"])
        endpoint = f"https://{url.netloc}"
        require(
            matches(HOST, url.netloc)
            and url.hostname
            and (url.port is None or 0 < url.port <= 65535)
            and target["url"] == endpoint,
            "INVALID_TARGET_ENDPOINT",
        )
        overrides = target.get("project_overrides", {})
        require(
            isinstance(overrides, dict) and matches(PROJECT, target["project"]),
            "INVALID_CONFIG",
        )
        for repo, project in overrides.items():
            require(
                matches(REPOSITORY, repo) and matches(PROJECT, project),
                "INVALID_CONFIG",
            )
        exclusive = run.get("exclusive_write", False)
        timeout = run.get("copy_timeout_seconds", 3600)
        require(
            type(exclusive) is bool and type(timeout) is int and 1 <= timeout <= 86400,
            "INVALID_CONFIG",
        )
        require(
            matches(re.compile(r"\d+\.\d+\.\d+"), run["skopeo_version"]),
            "INVALID_CONFIG",
        )
        return Config(
            doc["request_id"],
            source["account_id"],
            source["region"],
            profile,
            endpoint,
            target["organization_id"],
            target["installation_id"],
            target["project"],
            overrides,
            absolute_path(target["credentials_ref"]),
            absolute_path(target["ca_ref"]) if "ca_ref" in target else None,
            exclusive,
            run["skopeo_version"],
            timeout,
            absolute_path(run["state_dir"]),
        )
    except (OSError, ValueError, yaml.YAMLError):
        raise MigrationError("INVALID_CONFIG") from None


@dataclass(frozen=True)
class Robot:
    username: str = field(repr=False)
    secret: str = field(repr=False)


class HarborCredentials:
    def __init__(self, config: Config):
        self.config = config

    def account(self, project: str, role: str) -> Robot:
        # Reopen the file so a platform can replace expired credentials without editing the plan.
        try:
            fd = os.open(
                self.config.credentials_ref, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
            with os.fdopen(fd) as file:
                metadata = os.fstat(file.fileno())
                require(
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_uid == os.geteuid()
                    and not metadata.st_mode & 0o077
                    and metadata.st_size <= 4 * 1024 * 1024,
                    "UNSAFE_HARBOR_CREDENTIALS",
                )
                document = json.load(file)
            require(
                document["binding"]
                == {
                    "organization_id": self.config.organization,
                    "installation_id": self.config.installation,
                    "endpoint": self.config.endpoint,
                },
                "HARBOR_BINDING_MISMATCH",
            )
            account = document["accounts"][f"{project}/{role}"]
            now = time.time()
            require(document.get("expires_at", now + 1) > now, "HARBOR_HANDOFF_EXPIRED")
            require(
                account["project"] == project
                and account["role"] == role
                and account["pending"] is False
                and account["expires_at"] > now,
                "HARBOR_ACCOUNT_UNAVAILABLE",
            )
            username, secret = account["username"], account["secret"]
            require(
                isinstance(username, str)
                and username
                and ":" not in username
                and isinstance(secret, str)
                and secret,
                "INVALID_HARBOR_CREDENTIALS",
            )
            return Robot(username, secret)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise MigrationError("INVALID_HARBOR_CREDENTIALS") from None
