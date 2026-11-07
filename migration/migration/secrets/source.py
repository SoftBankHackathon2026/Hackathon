"""Read-only AWS collection. Preserve policy documents, never secret values, in inventory."""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase

from .common import (
    SecretsError,
    json_value,
    now,
    require,
    seal_artifact,
    source_fingerprint,
)
from .models import SecretValue


def silence():
    for name in ("boto3", "botocore", "urllib3"):
        for child_name in [name, *list(logging.Logger.manager.loggerDict)]:
            if child_name == name or child_name.startswith(name + "."):
                logger = logging.getLogger(child_name)
                logger.handlers[:] = [logging.NullHandler()]
                logger.propagate = False
                logger.disabled = True


class AwsSource:
    def __init__(self, config, *, sm=None, sts=None, kms=None):
        self.config = config
        silence()
        if sm is None:
            import boto3
            from botocore.config import Config

            session = boto3.Session(
                profile_name=config.get("profile"), region_name=config["region"]
            )
            options = Config(
                connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}
            )
            sm, sts, kms = (
                session.client(name, config=options)
                for name in ("secretsmanager", "sts", "kms")
            )
        self.sm, self.sts, self.kms = sm, sts, kms

    def call(self, client, operation, **kwargs):
        silence()
        try:
            return getattr(client, operation)(**kwargs)
        except Exception:
            raise SecretsError("SOURCE_READ_FAILED") from None

    def pages(self, operation, field, **kwargs):
        seen = set()
        while True:
            result = self.call(self.sm, operation, **kwargs)
            require(isinstance(result.get(field), list), "SOURCE_RESPONSE_INVALID")
            yield from result[field]
            token = result.get("NextToken")
            if not token:
                break
            require(
                isinstance(token, str) and token not in seen, "SOURCE_PAGINATION_LOOP"
            )
            seen.add(token)
            kwargs["NextToken"] = token

    def arn(self, value):
        parts = value.split(":", 6)
        require(
            len(parts) == 7
            and parts[0] == "arn"
            and parts[2] == "secretsmanager"
            and parts[3] == self.config["region"]
            and parts[4] == self.config["account_id"]
            and parts[5] == "secret"
            and parts[6],
            "SOURCE_IDENTITY_MISMATCH",
        )

    def identity(self):
        require(
            self.call(self.sts, "get_caller_identity")["Account"]
            == self.config["account_id"],
            "SOURCE_ACCOUNT_MISMATCH",
        )
        return {k: self.config[k] for k in ("account_id", "region")}

    def inventory(self):
        identity = self.identity()
        result = {
            "schema_version": 1,
            "kind": "secret_inventory",
            "collected_at": now(),
            "source": identity,
            "resources": [],
            "coverage": [],
            "limitations": [
                "NOT_AN_EFFECTIVE_AWS_AUTHORIZATION_EVALUATOR",
                "ORGANIZATION_AND_SESSION_POLICIES_REQUIRE_EXTERNAL_REVIEW",
                "KMS_GRANTS_REQUIRE_EXTERNAL_REVIEW",
                "NO_SECRET_VALUES_COLLECTED",
            ],
        }
        try:
            entries = list(
                self.pages("list_secrets", "SecretList", IncludePlannedDeletion=True)
            )
            result["coverage"].append(
                {"scope": "selected_secret_listing", "status": "collected"}
            )
        except SecretsError as error:
            entries = []
            result["coverage"].append(
                {
                    "scope": "selected_secret_listing",
                    "status": "unknown",
                    "reason": error.code,
                }
            )
        for entry in entries:
            name = entry["Name"]
            if not any(
                fnmatchcase(name, p) for p in self.config["include_names"]
            ) or any(
                fnmatchcase(name, p) for p in self.config.get("exclude_names", [])
            ):
                continue
            try:
                self.arn(entry["ARN"])
                detail = self.call(self.sm, "describe_secret", SecretId=entry["ARN"])
                self.arn(detail["ARN"])
                require(
                    detail["ARN"] == entry["ARN"] and detail["Name"] == name,
                    "SOURCE_RECREATED",
                )
                versions = []
                for v in self.pages(
                    "list_secret_version_ids",
                    "Versions",
                    SecretId=detail["ARN"],
                    IncludeDeprecated=True,
                ):
                    versions.append(
                        {
                            "id": v["VersionId"],
                            "stages": sorted(v.get("VersionStages", [])),
                        }
                    )
                require(
                    len({v["id"] for v in versions}) == len(versions),
                    "DUPLICATE_SOURCE_VERSION",
                )
                response = self.call(
                    self.sm, "get_resource_policy", SecretId=detail["ARN"]
                )
                raw = response.get("ResourcePolicy")
                resource_policy = json_value(raw) if raw else None
                require(
                    resource_policy is None or isinstance(resource_policy, dict),
                    "POLICY_DOCUMENT_INVALID",
                )
                key = {"id": detail.get("KmsKeyId"), "coverage": "aws_managed_key"}
                if key["id"]:
                    try:
                        metadata = self.call(self.kms, "describe_key", KeyId=key["id"])[
                            "KeyMetadata"
                        ]
                        policy = self.call(
                            self.kms,
                            "get_key_policy",
                            KeyId=key["id"],
                            PolicyName="default",
                        )["Policy"]
                        key.update(
                            arn=metadata["Arn"],
                            origin=metadata["Origin"],
                            state=metadata["KeyState"],
                            policy=json_value(policy),
                            coverage="collected",
                        )
                    except SecretsError:
                        key["coverage"] = "unknown"
                        result["coverage"].append(
                            {
                                "resource": detail["ARN"],
                                "scope": "kms_policy",
                                "status": "unknown",
                            }
                        )
                item = {
                    "arn": detail["ARN"],
                    "name": name,
                    "versions": sorted(versions, key=lambda v: v["id"]),
                    "description": detail.get("Description", ""),
                    "tags": sorted(detail.get("Tags", []), key=lambda t: t["Key"]),
                    "kms": key,
                    "resource_policy": resource_policy,
                    "rotation_enabled": bool(detail.get("RotationEnabled")),
                    "managed_by_service": detail.get("OwningService"),
                    "scheduled_deletion": bool(detail.get("DeletedDate")),
                }
                result["resources"].append(item)
                result["coverage"].append(
                    {
                        "resource": item["arn"],
                        "scope": "secret_metadata_and_resource_policy",
                        "status": "collected",
                    }
                )
            except SecretsError as error:
                result["coverage"].append(
                    {
                        "resource": entry["ARN"],
                        "scope": "secret_metadata_and_resource_policy",
                        "status": "unknown",
                        "reason": error.code,
                    }
                )
        result["resources"].sort(key=lambda r: r["arn"])
        require(
            len({r["arn"] for r in result["resources"]}) == len(result["resources"]),
            "DUPLICATE_SOURCE_SECRET",
        )
        result["status"] = (
            "partial"
            if any(r["status"] != "collected" for r in result["coverage"])
            else "complete"
        )
        result["source_fingerprint"] = source_fingerprint(result)
        return seal_artifact(result, "inventory_hash")

    def read(self, arn, version):
        self.arn(arn)
        self.identity()
        result = self.call(self.sm, "get_secret_value", SecretId=arn, VersionId=version)
        require(
            result.get("ARN") == arn and result.get("VersionId") == version,
            "SOURCE_READ_IDENTITY_MISMATCH",
        )
        if isinstance(result.get("SecretString"), str):
            return SecretValue(result["SecretString"].encode("utf-8"), "string")
        require(isinstance(result.get("SecretBinary"), bytes), "SOURCE_VALUE_INVALID")
        # boto3 already returns bytes: a second base64 decode would corrupt them.
        return SecretValue(result["SecretBinary"], "binary")
