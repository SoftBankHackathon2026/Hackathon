from __future__ import annotations

from migration.image_migration.config import DIGEST, MigrationError, matches, require
from migration.image_migration.registry import Registry
from migration.image_migration.source import ECR

from .config import Config


def error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


class Target(ECR):
    def __init__(self, config: Config, client=None, registry=None):
        super().__init__(config, client)
        self.registry_client = registry

    def settings(self) -> dict:
        config = self.config
        encryption = {"encryptionType": config.encryption}
        if config.kms_key is not None:
            encryption["kmsKey"] = config.kms_key
        return {
            "imageTagMutability": "IMMUTABLE" if config.tag_immutability else "MUTABLE",
            "imageScanningConfiguration": {"scanOnPush": config.scan_on_push},
            "encryptionConfiguration": encryption,
        }

    def repository(self, name: str) -> dict | None:
        try:
            response = self.client.describe_repositories(
                registryId=self.config.account, repositoryNames=[name]
            )
            (record,) = response["repositories"]
        except Exception as exc:
            if error_code(exc) == "RepositoryNotFoundException":
                return None
            raise MigrationError("ECR_REPOSITORY_LOOKUP_FAILED") from None
        require(
            record.get("repositoryName") == name
            and record.get("registryId") == self.config.account
            and record.get("repositoryUri") == f"{self.config.target_host}/{name}",
            "ECR_REPOSITORY_SCOPE_MISMATCH",
        )
        return record

    def differences(self, record: dict) -> list[str]:
        wanted = self.settings()
        found = {
            "imageTagMutability": record.get("imageTagMutability"),
            "imageScanningConfiguration": {
                "scanOnPush": record.get("imageScanningConfiguration", {}).get(
                    "scanOnPush"
                )
            },
            "encryptionConfiguration": {
                key: value
                for key, value in record.get("encryptionConfiguration", {}).items()
                if key in wanted["encryptionConfiguration"]
            },
        }
        return sorted(key for key in wanted if wanted[key] != found[key])

    def ensure_repository(self, name: str) -> str:
        if self.repository(name) is not None:
            return "existing"
        try:
            response = self.client.create_repository(
                registryId=self.config.account, repositoryName=name, **self.settings()
            )
        except Exception as exc:
            if error_code(exc) == "RepositoryAlreadyExistsException":
                return "existing"
            raise MigrationError("ECR_REPOSITORY_CREATE_FAILED") from None
        require(
            response["repository"]["repositoryName"] == name,
            "ECR_REPOSITORY_CREATE_FAILED",
        )
        return "created"

    def lookup(self, name: str, image_id: dict) -> str | None:
        try:
            response = self.client.describe_images(
                registryId=self.config.account,
                repositoryName=name,
                imageIds=[image_id],
            )
            (record,) = response["imageDetails"]
        except Exception as exc:
            if error_code(exc) in {
                "ImageNotFoundException",
                "RepositoryNotFoundException",
            }:
                return None
            raise MigrationError("ECR_IMAGE_LOOKUP_FAILED") from None
        identity = record.get("imageDigest")
        require(matches(DIGEST, identity), "INVALID_ECR_IMAGE")
        return identity

    def tag_digest(self, name: str, tag: str) -> str | None:
        return self.lookup(name, {"imageTag": tag})

    def has_digest(self, name: str, identity: str) -> bool:
        return self.lookup(name, {"imageDigest": identity}) == identity

    def verify_layer(self, name: str, identity: str, size: int) -> None:
        try:
            response = self.client.batch_check_layer_availability(
                registryId=self.config.account,
                repositoryName=name,
                layerDigests=[identity],
            )
            layers = response.get("layers", [])
            failures = response.get("failures", [])
        except Exception:
            raise MigrationError("BLOB_CHECK_FAILED") from None
        require(not failures and len(layers) == 1, "BLOB_UNAVAILABLE")
        (layer,) = layers
        require(
            layer.get("layerDigest") == identity
            and layer.get("layerAvailability") == "AVAILABLE",
            "BLOB_UNAVAILABLE",
        )
        require(layer.get("layerSize") == size, "BLOB_SIZE_MISMATCH")

    def registry(self, _project: str | None = None) -> Reader:
        if self.registry_client is None:
            self.registry_client = Registry(self.config.target_host, self.authorization)
        return Reader(self, self.registry_client)


class Reader:
    def __init__(self, target: Target, registry):
        self.target = target
        self.registry = registry

    def manifest(self, repo: str, reference: str):
        return self.registry.manifest(repo, reference)

    def verify_blob(self, repo: str, expected: str, size: int) -> None:
        self.target.verify_layer(repo, expected, size)


class TagReader:
    def __init__(self, target: Target, repositories: set[str]):
        self.target = target
        self.repositories = repositories
        self.found: dict[tuple[str, str], str] = {}

    def manifest(self, repo: str, reference: str):
        if repo not in self.repositories:
            return None
        identity = self.target.tag_digest(repo, reference)
        if identity is None:
            return None
        self.found[(repo, reference)] = identity
        return identity, None
