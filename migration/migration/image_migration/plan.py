from __future__ import annotations

from datetime import UTC, datetime

from .config import DIGEST, REPOSITORY, TAG, Config, MigrationError, matches, require
from .registry import IMAGE_CONFIGS, IMAGE_TYPES, INDEX_TYPES, child_descriptors

ATTACHMENT_TYPES = {
    "application/vnd.cncf.helm.config.v1+json",
    "application/vnd.dev.cosign.simplesigning.v1+json",
    "application/vnd.in-toto+json",
    "application/vnd.cncf.notary.signature",
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def image_kind(image: dict, manifest: dict | None = None) -> str:
    if image.get("subject"):
        return "attachment"
    if manifest is not None:
        media = manifest.get("mediaType")
        if manifest.get("subject") or manifest.get("artifactType"):
            return "attachment"
        if media in INDEX_TYPES:
            return "index"
        if media in IMAGE_TYPES:
            config = manifest.get("config")
            if isinstance(config, dict):
                config_type = config.get("mediaType")
                layers = manifest.get("layers", [])
                if config_type in ATTACHMENT_TYPES or (
                    isinstance(layers, list)
                    and bool(layers)
                    and all(
                        isinstance(layer, dict)
                        and layer.get("mediaType") in ATTACHMENT_TYPES
                        for layer in layers
                    )
                ):
                    return "attachment"
                if config_type in IMAGE_CONFIGS:
                    return "image"
        return "unsupported"

    media = image["media_type"]
    artifact = image["artifact_type"]
    if media in INDEX_TYPES:
        return "index"
    if media in IMAGE_TYPES:
        if artifact in ATTACHMENT_TYPES:
            return "attachment"
        if artifact in IMAGE_CONFIGS | {None}:
            return "image"
    return "unsupported"


def plan(config: Config, inventory: dict, source_registry, readers) -> dict:
    repositories = []
    for repo in inventory["repositories"]:
        name = repo["repository"]
        target = config.target(name)
        items = []
        children: set[str] = set()
        reader = None
        reader_error = None
        if repo["images"]:
            try:
                reader = readers(target.split("/", 1)[0])
            except MigrationError as exc:
                reader_error = exc.code
        for image in repo["images"]:
            identity = image["digest"]
            kind = image_kind(image)
            issues = []
            dependencies = []
            if image["state"] == "ACTIVE":
                try:
                    remote = source_registry.manifest(name, identity)
                    require(remote is not None, "SOURCE_MANIFEST_MISSING")
                    kind = image_kind(image, remote[1])
                    if kind == "index":
                        dependencies = sorted(
                            {child for child, _ in child_descriptors(remote[1])}
                        )
                        children.update(dependencies)
                except MigrationError as exc:
                    issues.append(exc.code)
            items.append(
                {
                    **image,
                    "tags": list(image["tags"]),
                    "kind": kind,
                    "issues": issues,
                    "children": dependencies,
                    "source_checked": image["state"] == "ACTIVE" and not issues,
                }
            )

        reserved = {tag for item in items for tag in item["tags"]}
        for item in items:
            identity = item["digest"]
            tags = list(item["tags"])
            if (
                item["kind"] in {"index", "image"}
                and not tags
                and identity not in children
            ):
                tag = "oneport-untagged-" + identity.replace(":", "-")
                require(tag not in reserved, "SYNTHETIC_TAG_COLLISION")
                tags.append(tag)
            item["target_tags"] = tags if item["kind"] != "attachment" else []
            if item["kind"] == "attachment":
                continue
            if item["state"] != "ACTIVE":
                item["issues"].append("SOURCE_RESTORE_PENDING")
            if item["kind"] == "unsupported":
                item["issues"].append("UNSUPPORTED_IMAGE_FORMAT")
            if reader_error:
                item["issues"].append(reader_error)
            if reader is not None:
                for tag in item["target_tags"]:
                    try:
                        existing = reader.manifest(target, tag)
                        if existing is not None and existing[0] != identity:
                            item["issues"].append("TARGET_TAG_CONFLICT")
                    except MigrationError as exc:
                        item["issues"].append(exc.code)
            item["issues"] = sorted(set(item["issues"]))
        repositories.append({"source": name, "target": target, "items": items})
    return {
        "schema_version": 1,
        "binding": config.binding,
        "created_at": now(),
        "estimated_bytes": sum(
            item["size"]
            for repo in repositories
            for item in repo["items"]
            if type(item["size"]) is int
            and item["size"] >= 0
            and item["kind"] != "attachment"
        ),
        "repositories": repositories,
    }


def validate_plan(config: Config, doc: dict) -> None:
    try:
        require(
            doc["schema_version"] == 1 and doc["binding"] == config.binding,
            "PLAN_BINDING_MISMATCH",
        )
        seen_repos: set[str] = set()
        for repo in doc["repositories"]:
            name = repo["source"]
            require(
                matches(REPOSITORY, name) and name not in seen_repos, "INVALID_PLAN"
            )
            seen_repos.add(name)
            require(repo["target"] == config.target(name), "PLAN_TARGET_MISMATCH")
            source_tags: set[str] = set()
            seen_digests: set[str] = set()
            seen_tags: set[str] = set()
            children: set[str] = set()
            for item in repo["items"]:
                require(
                    matches(DIGEST, item["digest"])
                    and item["digest"] not in seen_digests
                    and item["kind"] in {"image", "index", "attachment", "unsupported"}
                    and item["state"] in {"ACTIVE", "ARCHIVED", "ACTIVATING"}
                    and type(item["source_checked"]) is bool
                    and isinstance(item["children"], list)
                    and all(matches(DIGEST, child) for child in item["children"])
                    and (not item["children"] or item["kind"] == "index"),
                    "INVALID_PLAN",
                )
                seen_digests.add(item["digest"])
                children.update(item["children"])
                require(
                    isinstance(item["tags"], list)
                    and all(matches(TAG, tag) for tag in item["tags"])
                    and source_tags.isdisjoint(item["tags"]),
                    "INVALID_PLAN",
                )
                source_tags.update(item["tags"])
            for item in repo["items"]:
                expected = item["tags"] if item["kind"] != "attachment" else []
                if (
                    not expected
                    and item["kind"] in {"index", "image"}
                    and item["digest"] not in children
                ):
                    expected = ["oneport-untagged-" + item["digest"].replace(":", "-")]
                    require(source_tags.isdisjoint(expected), "SYNTHETIC_TAG_COLLISION")
                require(
                    item["target_tags"] == expected and seen_tags.isdisjoint(expected),
                    "INVALID_PLAN",
                )
                seen_tags.update(expected)
    except (KeyError, TypeError, AttributeError):
        raise MigrationError("INVALID_PLAN") from None


def drift(doc: dict, inventory: dict) -> dict:
    expected = {
        repo["source"]: {item["digest"]: item["tags"] for item in repo["items"]}
        for repo in doc["repositories"]
    }
    current = {
        repo["repository"]: {item["digest"]: item["tags"] for item in repo["images"]}
        for repo in inventory["repositories"]
    }
    changes = []
    for name in sorted(expected.keys() & current.keys()):
        before, after = expected[name], current[name]
        changes.extend(
            {
                "repository": name,
                "digest": identity,
                "before_tags": before.get(identity),
                "after_tags": after.get(identity),
            }
            for identity in sorted(before.keys() | after.keys())
            if before.get(identity) != after.get(identity)
        )
    return {
        "added_repositories": sorted(current.keys() - expected.keys()),
        "removed_repositories": sorted(expected.keys() - current.keys()),
        "changed_images": changes,
    }
