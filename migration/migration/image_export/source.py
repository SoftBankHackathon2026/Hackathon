from __future__ import annotations

import http.client
import json
from contextlib import closing
from urllib.parse import quote, urlencode

from migration.image_migration.config import (
    DIGEST,
    REPOSITORY,
    TAG,
    MigrationError,
    matches,
    require,
)
from migration.image_migration.registry import Registry, encoded_robot

from .config import Config

PAGE_SIZE = 100
MAX_PAGES = 10000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class Harbor:
    def __init__(self, config: Config, registry=None):
        self.config = config
        self.registry = registry or Registry(
            config.source_host, self.authorization, config.ca_ref
        )

    def authorization(self) -> str:
        return encoded_robot(self.config.robot())

    def get(self, path: str, query: dict) -> tuple[list, int | None]:
        url = f"{self.config.endpoint}/api/v2.0{path}?{urlencode(query)}"
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic " + self.authorization(),
        }
        try:
            with closing(self.registry.open(url, headers)) as response:
                if response.code in {401, 403}:
                    raise MigrationError("HARBOR_API_FORBIDDEN")
                if response.code == 404:
                    raise MigrationError("HARBOR_PROJECT_NOT_FOUND")
                if response.code in {429, 500, 502, 503, 504}:
                    raise MigrationError("HARBOR_API_TEMPORARY_FAILURE")
                require(response.code == 200, "HARBOR_API_FAILED")
                data = response.read(MAX_RESPONSE_BYTES + 1)
                require(len(data) <= MAX_RESPONSE_BYTES, "HARBOR_RESPONSE_TOO_LARGE")
                total = response.headers.get("X-Total-Count")
            rows = json.loads(data)
            require(isinstance(rows, list), "INVALID_HARBOR_RESPONSE")
            return rows, int(total) if total is not None else None
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            raise MigrationError("INVALID_HARBOR_RESPONSE") from None

    def pages(self, path: str, query: dict) -> list:
        rows: list = []
        total = None
        for page in range(1, MAX_PAGES + 1):
            batch, total = self.get(
                path, {**query, "page": page, "page_size": PAGE_SIZE}
            )
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
        else:
            raise MigrationError("HARBOR_LIST_INCOMPLETE")
        require(total is None or total == len(rows), "HARBOR_INVENTORY_CHANGED")
        return rows

    def inventory(self) -> dict:
        repositories = []
        seen: set[str] = set()
        for project in self.config.projects:
            for record in self.pages(
                f"/projects/{quote(project, safe='')}/repositories", {}
            ):
                name = record.get("name") if isinstance(record, dict) else None
                require(
                    matches(REPOSITORY, name)
                    and name.startswith(project + "/")
                    and name not in seen,
                    "INVALID_HARBOR_REPOSITORY",
                )
                seen.add(name)
                repositories.append(
                    {"repository": name, "images": self.images(project, name)}
                )
        return {
            "schema_version": 1,
            "endpoint": self.config.endpoint,
            "projects": list(self.config.projects),
            "repositories": sorted(repositories, key=lambda repo: repo["repository"]),
        }

    def images(self, project: str, name: str) -> list[dict]:
        short = quote(quote(name[len(project) + 1 :], safe=""), safe="")
        base = f"/projects/{quote(project, safe='')}/repositories/{short}/artifacts"
        images: dict[str, dict] = {}
        tags: set[str] = set()
        accessories = []
        for record in self.pages(
            base,
            {
                "with_tag": "false",
                "with_label": "false",
                "with_scan_overview": "false",
                "with_accessory": "true",
            },
        ):
            require(isinstance(record, dict), "INVALID_HARBOR_ARTIFACT")
            identity = record.get("digest")
            require(
                matches(DIGEST, identity) and identity not in images,
                "INVALID_HARBOR_ARTIFACT",
            )
            names = [
                tag.get("name") if isinstance(tag, dict) else None
                for tag in self.pages(f"{base}/{identity}/tags", {})
            ]
            require(
                all(matches(TAG, tag) for tag in names)
                and len(set(names)) == len(names)
                and tags.isdisjoint(names),
                "INVALID_HARBOR_ARTIFACT",
            )
            tags.update(names)
            images[identity] = artifact(record, identity, sorted(names), None)
            for accessory in record.get("accessories") or []:
                accessories.append((accessory, identity))
        for accessory, subject in accessories:
            identity = accessory.get("digest") if isinstance(accessory, dict) else None
            require(matches(DIGEST, identity), "INVALID_HARBOR_ARTIFACT")
            images.setdefault(identity, artifact(accessory, identity, [], subject))
        return sorted(images.values(), key=lambda image: image["digest"])


def artifact(record: dict, identity: str, tags: list[str], subject) -> dict:
    media = record.get("manifest_media_type") or record.get("media_type")
    kind = record.get("artifact_type")
    size = record.get("size")
    return {
        "digest": identity,
        "tags": tags,
        "state": "ACTIVE",
        "media_type": media if isinstance(media, str) else None,
        "artifact_type": kind if isinstance(kind, str) else None,
        "subject": subject,
        "size": size if type(size) is int and size >= 0 else None,
    }
