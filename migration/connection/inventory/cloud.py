"""Discover managed Kubernetes through the local, already authenticated CLI."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from .collector import (
    MAX_CONTEXTS,
    InventoryError,
    Kubectl,
    collect,
    decode_json,
    run_bounded,
)
from .schema import server_url, target_id

CLOUD_PROVIDERS = ("aws", "gcp")
DISCOVERY_CODES = {
    "CLI_MISSING",
    "LOGIN_REQUIRED",
    "PERMISSION_DENIED",
    "REGION_REQUIRED",
    "SCOPE_LIMIT",
    "LOOKUP_FAILED",
}
_SCOPE = r"[a-z0-9][a-z0-9-]{0,62}"
_NAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"
CONTEXT = {
    "aws": re.compile(rf"([0-9]{{12}})/({_SCOPE})/({_NAME})\Z"),
    "gcp": re.compile(rf"({_SCOPE})/({_SCOPE})/({_NAME})\Z"),
}


def classify_failure(stderr: bytes) -> str:
    message = stderr.lower()
    if any(
        s in message
        for s in (
            b"expiredtoken",
            b"invalidclienttokenid",
            b"unrecognizedclientexception",
            b"unable to locate credentials",
            b"unauthenticated",
            b"invalid_grant",
            b"reauthentication",
            b"no active account",
            b"login session",
            b"error loading sso token",
            b"token has expired",
            b"code=401",
        )
    ):
        return "LOGIN_REQUIRED"
    if any(
        s in message
        for s in (
            b"accessdenied",
            b"unauthorizedoperation",
            b"permission_denied",
            b"does not have permission",
            b"required permission",
            b"forbidden",
            b"code=403",
        )
    ):
        return "PERMISSION_DENIED"
    return "LOOKUP_FAILED"


def lookup_error(error: Exception) -> str:
    if isinstance(error, InventoryError) and error.code in DISCOVERY_CODES:
        return error.code
    return "LOOKUP_FAILED"


class Cloud:
    def __init__(self, provider: str, *, runner=run_bounded):
        self.provider = provider
        self.runner = runner
        self.binary = shutil.which("aws" if provider == "aws" else "gcloud")
        if not self.binary:
            raise InventoryError("CLI_MISSING")
        self.deadline = time.monotonic() + 50

    def command(self, *args: str) -> bytes:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise InventoryError("LOOKUP_FAILED")
        flags = (
            ["--no-cli-pager", "--no-cli-auto-prompt"]
            if self.provider == "aws"
            else ["--quiet"]
        )
        return self.runner(
            [self.binary, *args, *flags],
            timeout=min(remaining, 7),
            classify_failure=classify_failure,
        )

    def json(self, *args: str):
        flags = ["--output", "json"] if self.provider == "aws" else ["--format=json"]
        return decode_json(self.command(*args, *flags))

    def account(self, region: str) -> str:
        account = self.json("sts", "get-caller-identity", "--region", region)["Account"]
        if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
            raise InventoryError("LOOKUP_FAILED")
        return account

    def region(self) -> str:
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            try:
                region = self.command("configure", "get", "region").decode().strip()
            except InventoryError:
                raise InventoryError("REGION_REQUIRED") from None
        if not re.fullmatch(_SCOPE, region):
            raise InventoryError("REGION_REQUIRED")
        return region

    def active_gcp_account(self) -> None:
        if not self.json("auth", "list", "--filter=status:ACTIVE"):
            raise InventoryError("LOGIN_REQUIRED")

    def describe(self, context: str) -> tuple[dict, str, str]:
        match = CONTEXT[self.provider].fullmatch(context)
        if not match:
            raise InventoryError("LOOKUP_FAILED")
        scope, location, name = match.groups()
        if self.provider == "aws":
            details = self.json(
                "eks", "describe-cluster", "--region", location, "--name", name
            )["cluster"]
            arn = details["arn"]
            if not re.fullmatch(
                rf"arn:aws(?:-[a-z-]+)?:eks:{location}:{scope}:cluster/{re.escape(name)}",
                arn,
            ):
                raise InventoryError("LOOKUP_FAILED")
            endpoint, ca = details["endpoint"], details["certificateAuthority"]["data"]
        else:
            details = self.json(
                "container",
                "clusters",
                "describe",
                name,
                "--location",
                location,
                "--project",
                scope,
            )
            if details["name"] != name or details["location"] != location:
                raise InventoryError("LOOKUP_FAILED")
            endpoint, ca = (
                "https://" + details["endpoint"],
                details["masterAuth"]["clusterCaCertificate"],
            )
        endpoint = server_url(endpoint)
        certificate = base64.b64decode(ca, validate=True)
        if b"-----BEGIN CERTIFICATE-----" not in certificate:
            raise InventoryError("LOOKUP_FAILED")
        trust = "sha384:" + hashlib.sha384(certificate).hexdigest()
        return (
            {
                "id": target_id(context, endpoint, trust, ""),
                "kind": self.provider,
                "name": name,
                "identity": context,
                "region": location,
                "context": context,
            },
            endpoint,
            ca,
        )

    def list_scope(self, scope: str, region: str = "") -> list[str]:
        if self.provider == "aws":
            names = self.json(
                "eks",
                "list-clusters",
                "--region",
                region,
                "--max-items",
                str(MAX_CONTEXTS + 1),
            )["clusters"]
            return [f"{scope}/{region}/{name}" for name in names]
        clusters = self.json(
            "container",
            "clusters",
            "list",
            "--project",
            scope,
            "--limit",
            str(MAX_CONTEXTS + 1),
        )
        return [f"{scope}/{item['location']}/{item['name']}" for item in clusters]


def discover_cloud(provider: str) -> dict:
    warnings: list[dict] = []

    def warn(code: str, scope: str = "") -> None:
        item = {"code": code, "scope": scope}
        if item not in warnings:
            warnings.append(item)

    def inspect(context: str):
        try:
            return cloud.describe(context)[0], None
        except (InventoryError, KeyError, TypeError, ValueError) as error:
            return None, lookup_error(error)

    def list_project(project: str):
        try:
            return cloud.list_scope(project), None
        except (InventoryError, KeyError, TypeError, ValueError) as error:
            return [], lookup_error(error)

    discovered = []
    try:
        cloud = Cloud(provider)
        with ThreadPoolExecutor(max_workers=8) as pool:
            if provider == "aws":
                region = cloud.region()
                account = cloud.account(region)
                try:
                    contexts = cloud.list_scope(account, region)
                except (InventoryError, KeyError, TypeError, ValueError) as error:
                    warn(lookup_error(error), f"{account}/{region}")
                    contexts = []
            else:
                cloud.active_gcp_account()
                # An explicitly configured project can be queryable even when project listing is restricted.
                try:
                    configured = (
                        cloud.command("config", "get-value", "project").decode().strip()
                    )
                except InventoryError:
                    configured = ""
                projects = [configured] if re.fullmatch(_SCOPE, configured) else []
                try:
                    visible = cloud.json(
                        "projects", "list", "--limit", str(MAX_CONTEXTS + 1)
                    )
                    projects.extend(
                        p["projectId"]
                        for p in visible
                        if re.fullmatch(_SCOPE, p["projectId"])
                    )
                except (InventoryError, KeyError, TypeError, ValueError) as error:
                    warn(lookup_error(error))
                projects = list(dict.fromkeys(projects))
                if len(projects) > MAX_CONTEXTS:
                    warn("SCOPE_LIMIT")
                contexts = []
                for project, (items, error) in zip(
                    projects, pool.map(list_project, projects[:MAX_CONTEXTS])
                ):
                    if error:
                        warn(error, project)
                    contexts.extend(items)
            contexts = list(
                dict.fromkeys(c for c in contexts if CONTEXT[provider].fullmatch(c))
            )
            if len(contexts) > MAX_CONTEXTS:
                warn("SCOPE_LIMIT")
            contexts = contexts[:MAX_CONTEXTS]
            for context, (candidate, error) in zip(
                contexts, pool.map(inspect, contexts)
            ):
                if error:
                    warn(error, context)
                else:
                    discovered.append(candidate)
    except (InventoryError, KeyError, TypeError, ValueError) as error:
        warn(lookup_error(error))
    if len(warnings) > 20:
        warnings = warnings[:19] + [{"code": "SCOPE_LIMIT", "scope": ""}]
    # Keep the signed relay document within its transport limit even for long names.
    while (
        len(json.dumps({"environments": discovered, "warnings": warnings}).encode())
        > 23_000
    ):
        discovered.pop()
        warn("SCOPE_LIMIT")
    return {"discovered": discovered, "warnings": warnings[:20]}


def collect_cloud(provider: str, context: str, selected_id: str, **binding) -> dict:
    cloud = Cloud(provider)
    match = CONTEXT[provider].fullmatch(context)
    if not match:
        raise InventoryError("SELECTED_TARGET_CHANGED_OR_MISSING")
    scope, location, name = match.groups()
    if provider == "aws" and cloud.account(location) != scope:
        raise InventoryError("SELECTED_TARGET_CHANGED_OR_MISSING")
    candidate, endpoint, ca = cloud.describe(context)
    if candidate["id"] != selected_id:
        raise InventoryError("SELECTED_TARGET_CHANGED_OR_MISSING")
    if provider == "aws":
        command = cloud.binary
        args = [
            "eks",
            "get-token",
            "--region",
            location,
            "--cluster-name",
            name,
            "--output",
            "json",
            "--no-cli-pager",
            "--no-cli-auto-prompt",
        ]
    else:
        command = shutil.which("gke-gcloud-auth-plugin")
        args = []
        if not command:
            raise InventoryError("GKE_AUTH_PLUGIN_MISSING")
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "clusters": [
            {
                "name": context,
                "cluster": {"server": endpoint, "certificate-authority-data": ca},
            }
        ],
        "contexts": [
            {"name": context, "context": {"cluster": context, "user": context}}
        ],
        "users": [
            {
                "name": context,
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": str(Path(command).resolve()),
                        "args": args,
                        "interactiveMode": "Never",
                    }
                },
            }
        ],
    }
    with TemporaryDirectory(prefix="migration-cloud-") as directory:
        path = Path(directory) / "config"
        with os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as output:
            json.dump(config, output)
        return collect(
            Kubectl(kubeconfig=str(path)),
            selected_target_id=selected_id,
            cloud_environment=candidate,
            **binding,
        )
