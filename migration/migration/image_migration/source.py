from __future__ import annotations

import base64
import time

from .config import DIGEST, REPOSITORY, TAG, Config, MigrationError, matches, require


class ECR:
    def __init__(self, config: Config, client=None):
        self.config = config
        self.auth: tuple[str, float] | None = None
        if client is None:
            try:
                import boto3
                from botocore.config import Config as AWSConfig

                client = boto3.Session(profile_name=config.profile).client(
                    "ecr",
                    region_name=config.region,
                    config=AWSConfig(
                        ignore_configured_endpoint_urls=True,
                        connect_timeout=10,
                        read_timeout=30,
                        retries={"mode": "standard", "total_max_attempts": 5},
                    ),
                )
            except ImportError:
                raise MigrationError("INSTALL_IMAGE_MIGRATION_EXTRA") from None
            except Exception:
                raise MigrationError("ECR_CLIENT_UNAVAILABLE") from None
        self.client = client

    def authorization(self, *, refresh: bool = False) -> str:
        if not refresh and self.auth is not None and self.auth[1] > time.time() + 300:
            return self.auth[0]
        try:
            (item,) = self.client.get_authorization_token()["authorizationData"]
            value, expires = item["authorizationToken"], item["expiresAt"].timestamp()
            valid = base64.b64decode(value, validate=True).startswith(b"AWS:")
            require(valid and expires > time.time(), "ECR_AUTH_UNAVAILABLE")
        except Exception:
            raise MigrationError("ECR_AUTH_UNAVAILABLE") from None
        self.auth = value, expires
        return value

    def inventory(self) -> dict:
        config = self.config
        account = {"registryId": config.account}
        repositories: list[dict] = []
        seen: set[str] = set()
        try:
            pages = self.client.get_paginator("describe_repositories").paginate(
                **account
            )
            for page in pages:
                for record in page["repositories"]:
                    name = record["repositoryName"]
                    require(
                        matches(REPOSITORY, name) and name not in seen,
                        "ECR_INVENTORY_CHANGED",
                    )
                    seen.add(name)
                    require(
                        record["registryId"] == config.account
                        and record["repositoryUri"] == f"{config.source_host}/{name}",
                        "ECR_REPOSITORY_SCOPE_MISMATCH",
                    )
                    repositories.append(
                        {"repository": name, "images": self.images(name)}
                    )
        except MigrationError:
            raise
        except Exception:
            raise MigrationError("ECR_REPOSITORY_LIST_INCOMPLETE") from None
        return {
            "schema_version": 1,
            "account_id": config.account,
            "region": config.region,
            "repositories": sorted(repositories, key=lambda repo: repo["repository"]),
        }

    def images(self, repository: str) -> list[dict]:
        images: dict[str, dict] = {}
        tags: set[str] = set()
        try:
            pages = self.client.get_paginator("describe_images").paginate(
                registryId=self.config.account,
                repositoryName=repository,
                filter={"imageStatus": "ANY", "tagStatus": "ANY"},
            )
            for page in pages:
                for record in page["imageDetails"]:
                    identity = record["imageDigest"]
                    names = record.get("imageTags", [])
                    state = record.get("imageStatus", "ACTIVE")
                    require(
                        record["registryId"] == self.config.account
                        and record["repositoryName"] == repository
                        and matches(DIGEST, identity)
                        and state in {"ACTIVE", "ARCHIVED", "ACTIVATING"}
                        and isinstance(names, list)
                        and all(matches(TAG, name) for name in names),
                        "INVALID_ECR_IMAGE",
                    )
                    require(
                        identity not in images and tags.isdisjoint(names),
                        "ECR_INVENTORY_CHANGED",
                    )
                    tags.update(names)
                    images[identity] = {
                        "digest": identity,
                        "tags": sorted(set(names)),
                        "state": state,
                        "media_type": record.get("imageManifestMediaType"),
                        "artifact_type": record.get("artifactMediaType"),
                        "subject": record.get("subjectManifestDigest"),
                        "size": record.get("imageSizeInBytes"),
                    }
        except MigrationError:
            raise
        except Exception:
            raise MigrationError("ECR_IMAGE_LIST_INCOMPLETE") from None
        return list(sorted(images.values(), key=lambda image: image["digest"]))
