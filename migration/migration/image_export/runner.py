from __future__ import annotations

import time

from migration.image_migration.config import MigrationError, require
from migration.image_migration.registry import encoded_robot
from migration.image_migration.runner import (
    address,
    archive_dependencies,
    outcome,
    record,
    recorded,
)

from .config import Config

RETRYABLE = {
    "COPY_AUTH_FAILED",
    "COPY_TEMPORARY_FAILURE",
    "COPY_TIMED_OUT",
    "COPY_RESULT_UNCONFIRMED",
    "ECR_IMAGE_LOOKUP_FAILED",
    "REGISTRY_CONNECTION_FAILED",
    "REGISTRY_TEMPORARY_FAILURE",
}
BLOCKING = {
    "TARGET_TAG_CONFLICT",
    "SOURCE_IMAGE_MISSING",
    "UNSUPPORTED_IMAGE_FORMAT",
    "SOURCE_FORMAT_CHANGED_REPLAN",
    "TARGET_CHILD_MISSING",
    "SOURCE_INDEX_REPLAN_REQUIRED",
    "ECR_REPOSITORY_SCOPE_MISMATCH",
}


def copy_reference(config: Config, harbor, target, copier, repo, identity, tag):
    name = repo["target"]
    for attempt in range(3):
        try:
            previous = target.tag_digest(name, tag)
            if previous is not None:
                require(previous == identity, "TARGET_TAG_CONFLICT")
                return "already_present" if attempt == 0 else "present_after_retry"
            copier.transfer(
                f"{config.source_host}/{repo['source']}@{identity}",
                f"{config.target_host}/{name}:{tag}",
                (config.source_host, encoded_robot(config.robot())),
                (config.target_host, target.authorization(refresh=attempt > 0)),
                source_ca=config.ca_ref,
            )
            require(target.tag_digest(name, tag) == identity, "COPY_RESULT_UNCONFIRMED")
            return "copied"
        except MigrationError as exc:
            if exc.code not in RETRYABLE or attempt == 2:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def ensure_repositories(doc: dict, target, connection) -> dict[str, dict]:
    states = {}
    for repo in doc["repositories"]:
        name = repo["target"]
        status, reason = "failed", None
        try:
            status = target.ensure_repository(name)
        except MigrationError as exc:
            reason = exc.code
        record(connection, "repository", name, status, reason)
        states[name] = {"target_repository": name, "status": status, "reason": reason}
    return states


def apply(config: Config, doc: dict, harbor, target, copier, connection) -> dict:
    active = {
        repo["repository"]: {item["digest"]: item for item in repo["images"]}
        for repo in harbor.inventory()["repositories"]
    }
    repositories = ensure_repositories(doc, target, connection)
    results = []
    for repo in doc["repositories"]:
        current = active.get(repo["source"], {})
        repository_error = repositories[repo["target"]]["reason"]
        items = sorted(
            repo["items"],
            key=lambda item: (
                not item["target_tags"],
                item["kind"] != "index",
                item["digest"],
            ),
        )
        for item in items:
            identity = item["digest"]
            kind = item["kind"]
            if kind == "attachment":
                results.append(
                    {**outcome(repo, item, None), "status": "excluded_attachment"}
                )
                continue
            dependency_error = repository_error
            if dependency_error is None:
                try:
                    require(kind != "unsupported", "UNSUPPORTED_IMAGE_FORMAT")
                    require(identity in current, "SOURCE_IMAGE_MISSING")
                    if kind == "index" or not item["source_checked"]:
                        archive_dependencies(
                            item, current, harbor.registry, repo["source"]
                        )
                except MigrationError as exc:
                    dependency_error = exc.code
            for tag in item["target_tags"] or [None]:
                reference = address(config, repo, item, tag)
                status, reason = "failed", None
                previous = recorded(connection, "apply", reference)
                try:
                    if dependency_error:
                        raise MigrationError(dependency_error)
                    if tag is None:
                        require(
                            target.has_digest(repo["target"], identity),
                            "TARGET_CHILD_MISSING",
                        )
                        status = "present_via_index"
                    else:
                        status = copy_reference(
                            config, harbor, target, copier, repo, identity, tag
                        )
                except MigrationError as exc:
                    reason = exc.code
                    if reason in BLOCKING:
                        status = "blocked"
                record(connection, "apply", reference, status, reason)
                results.append(
                    {
                        **outcome(repo, item, tag),
                        "target_address": reference,
                        "status": status,
                        "reason": reason,
                        "previous_status": previous,
                    }
                )
    failed = any(row["status"] in {"failed", "blocked"} for row in results) or any(
        row["status"] == "failed" for row in repositories.values()
    )
    return {
        "status": "partial" if failed else "copied",
        "repositories": list(repositories.values()),
        "items": results,
    }
