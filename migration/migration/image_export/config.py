from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from migration.image_migration.config import (
    HOST,
    IDENTIFIER,
    PROJECT,
    MigrationError,
    Robot,
    absolute_path,
    matches,
    object_keys,
    require,
)

ECR_REPOSITORY = re.compile(
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*"
)
ACCOUNT = re.compile(r"[0-9]{12}")
REGION = re.compile(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
ROBOT_NAME = re.compile(r"[A-Za-z0-9$+._-]{1,255}")
KMS_KEY = re.compile(r"[A-Za-z0-9:/_+=.@-]{1,2048}")
ENCRYPTION = {"AES256", "KMS"}


@dataclass(frozen=True)
class Config:
    request_id: str
    endpoint: str
    projects: tuple[str, ...]
    robot_username: str
    secret_kind: str
    secret_ref: str
    ca_ref: Path | None
    account: str
    region: str
    profile: str | None
    repository_prefix: str | None
    tag_immutability: bool
    scan_on_push: bool
    encryption: str
    kms_key: str | None
    exclusive_write: bool
    skopeo_version: str
    copy_timeout: int
    state_dir: Path

    @property
    def source_host(self) -> str:
        return urlsplit(self.endpoint).netloc

    @property
    def target_host(self) -> str:
        suffix = (
            "amazonaws.com.cn" if self.region.startswith("cn-") else "amazonaws.com"
        )
        return f"{self.account}.dkr.ecr.{self.region}.{suffix}"

    @property
    def binding(self) -> dict:
        return {
            "request_id": self.request_id,
            "endpoint": self.endpoint,
            "projects": list(self.projects),
            "account": self.account,
            "region": self.region,
            "repository_prefix": self.repository_prefix,
            "tag_immutability": self.tag_immutability,
            "scan_on_push": self.scan_on_push,
            "encryption": self.encryption,
            "kms_key": self.kms_key,
        }

    def target(self, repo: str) -> str:
        target = f"{self.repository_prefix}/{repo}" if self.repository_prefix else repo
        require(
            2 <= len(target) <= 256 and matches(ECR_REPOSITORY, target),
            "INVALID_TARGET_REPOSITORY",
        )
        return target

    def robot(self) -> Robot:
        if self.secret_kind == "env":
            secret = os.environ.get(self.secret_ref, "")
        else:
            secret = read_secret_file(Path(self.secret_ref))
        require(bool(secret) and "\n" not in secret, "INVALID_HARBOR_CREDENTIALS")
        return Robot(self.robot_username, secret)


def read_secret_file(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as file:
            metadata = os.fstat(file.fileno())
            require(
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and not metadata.st_mode & 0o077
                and metadata.st_size <= 64 * 1024,
                "UNSAFE_HARBOR_CREDENTIALS",
            )
            return file.read().rstrip("\r\n")
    except (OSError, ValueError):
        raise MigrationError("INVALID_HARBOR_CREDENTIALS") from None


def secret_reference(value: object) -> tuple[str, str]:
    ref = object_keys(value, set(), {"file", "env"})
    require(len(ref) == 1, "INVALID_CONFIG")
    if "env" in ref:
        require(matches(ENV_NAME, ref["env"]), "INVALID_CONFIG")
        return "env", ref["env"]
    return "file", str(absolute_path(ref["file"]))


def load_config(path: Path) -> Config:
    try:
        doc = object_keys(
            yaml.safe_load(path.read_text()),
            {"schema_version", "request_id", "source", "target", "execution"},
        )
        source = object_keys(doc["source"], {"url", "projects", "robot"}, {"ca_ref"})
        robot = object_keys(source["robot"], {"username", "secret_ref"})
        target = object_keys(
            doc["target"],
            {"account_id", "region"},
            {"profile", "repository", "repository_prefix"},
        )
        repository = object_keys(
            target.get("repository", {}),
            set(),
            {"tag_immutability", "scan_on_push", "encryption", "kms_key"},
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
        require(matches(IDENTIFIER, doc["request_id"]), "INVALID_CONFIG")
        require(isinstance(source["url"], str), "INVALID_CONFIG")
        url = urlsplit(source["url"])
        endpoint = f"https://{url.netloc}"
        require(
            matches(HOST, url.netloc)
            and url.hostname
            and (url.port is None or 0 < url.port <= 65535)
            and source["url"] == endpoint,
            "INVALID_SOURCE_ENDPOINT",
        )
        projects = source["projects"]
        require(
            isinstance(projects, list)
            and projects
            and all(matches(PROJECT, project) for project in projects)
            and len(set(projects)) == len(projects),
            "INVALID_CONFIG",
        )
        require(matches(ROBOT_NAME, robot["username"]), "INVALID_CONFIG")
        require(":" not in robot["username"], "INVALID_CONFIG")
        secret_kind, secret_ref = secret_reference(robot["secret_ref"])
        require(matches(ACCOUNT, target["account_id"]), "INVALID_CONFIG")
        require(matches(REGION, target["region"]), "INVALID_CONFIG")
        profile = target.get("profile")
        require(profile is None or matches(IDENTIFIER, profile), "INVALID_CONFIG")
        prefix = target.get("repository_prefix")
        require(
            prefix is None or (matches(ECR_REPOSITORY, prefix) and len(prefix) <= 128),
            "INVALID_CONFIG",
        )
        immutable = repository.get("tag_immutability", True)
        scan = repository.get("scan_on_push", True)
        encryption = repository.get("encryption", "AES256")
        kms_key = repository.get("kms_key")
        require(
            type(immutable) is bool
            and type(scan) is bool
            and encryption in ENCRYPTION
            and (
                kms_key is None or (encryption == "KMS" and matches(KMS_KEY, kms_key))
            ),
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
            endpoint,
            tuple(projects),
            robot["username"],
            secret_kind,
            secret_ref,
            absolute_path(source["ca_ref"]) if "ca_ref" in source else None,
            target["account_id"],
            target["region"],
            profile,
            prefix,
            immutable,
            scan,
            encryption,
            kms_key,
            exclusive,
            run["skopeo_version"],
            timeout,
            absolute_path(run["state_dir"]),
        )
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise MigrationError("INVALID_CONFIG") from None
