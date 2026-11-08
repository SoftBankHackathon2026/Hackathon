from __future__ import annotations

import re

from migration.secrets.common import SecretsError, require
from migration.secrets.models import SecretValue
from migration.secrets.source import silence

ERRORS = {
    "ResourceNotFoundException": "TARGET_NOT_FOUND",
    "AccessDeniedException": "TARGET_ACCESS_DENIED",
    "AccessDenied": "TARGET_ACCESS_DENIED",
    "ResourceExistsException": "TARGET_WRITE_CONFLICT",
    "InvalidRequestException": "TARGET_WRITE_CONFLICT",
    "LimitExceededException": "TARGET_LIMIT_EXCEEDED",
    "EncryptionFailure": "TARGET_KMS_FAILED",
    "DecryptionFailure": "TARGET_KMS_FAILED",
    "UnrecognizedClientException": "TARGET_AUTHENTICATION_FAILED",
    "ExpiredTokenException": "TARGET_AUTHENTICATION_FAILED",
    "InvalidClientTokenId": "TARGET_AUTHENTICATION_FAILED",
}
ARN = re.compile(
    r"arn:aws[a-z-]*:secretsmanager:([a-z0-9-]+):(\d{12}):secret:(.+)-[A-Za-z0-9]{6}"
)
DESCRIPTION = "Copied from OpenBao KV v2 by migration.secrets_export"


def error_code(error):
    response = getattr(error, "response", None)
    detail = response.get("Error") if isinstance(response, dict) else None
    code = detail.get("Code") if isinstance(detail, dict) else None
    return ERRORS.get(code, "TARGET_UNAVAILABLE")


def tag_list(tags):
    return [{"Key": k, "Value": v} for k, v in sorted(tags.items())]


class SecretsManagerTarget:
    def __init__(self, config, *, sm=None, sts=None):
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
            sm, sts = (
                session.client(name, config=options)
                for name in ("secretsmanager", "sts")
            )
        self.sm, self.sts = sm, sts

    def __repr__(self):
        return "<SecretsManagerTarget [REDACTED]>"

    def call(self, client, operation, **kwargs):
        silence()
        try:
            result = getattr(client, operation)(**kwargs)
        except Exception as error:
            raise SecretsError(error_code(error)) from None
        require(isinstance(result, dict), "TARGET_RESPONSE_INVALID")
        return result

    def identity(self):
        account = self.call(self.sts, "get_caller_identity").get("Account")
        require(account == self.config["account_id"], "TARGET_ACCOUNT_MISMATCH")
        return (
            "aws-secretsmanager:"
            + self.config["account_id"]
            + ":"
            + self.config["region"]
        )

    def check_arn(self, arn, name):
        match = ARN.fullmatch(arn) if isinstance(arn, str) else None
        require(
            match is not None
            and match.group(1) == self.config["region"]
            and match.group(2) == self.config["account_id"]
            and match.group(3) == name,
            "TARGET_IDENTITY_MISMATCH",
        )
        return arn

    def describe(self, name):
        try:
            detail = self.call(self.sm, "describe_secret", SecretId=name)
        except SecretsError as error:
            if error.code == "TARGET_NOT_FOUND":
                return None
            raise
        require(detail.get("Name") == name, "TARGET_NAME_MISMATCH")
        self.check_arn(detail.get("ARN"), name)
        stages = detail.get("VersionIdsToStages") or {}
        tags = detail.get("Tags") or []
        require(
            isinstance(stages, dict)
            and all(isinstance(v, list) for v in stages.values())
            and isinstance(tags, list),
            "TARGET_RESPONSE_INVALID",
        )
        current = [v for v, labels in stages.items() if "AWSCURRENT" in labels]
        require(len(current) <= 1, "TARGET_CURRENT_AMBIGUOUS")
        return {
            "arn": detail["ARN"],
            "current_version": current[0] if current else None,
            "pending": any("AWSPENDING" in labels for labels in stages.values()),
            "tags": {t.get("Key"): t.get("Value") for t in tags},
            "scheduled_deletion": bool(detail.get("DeletedDate")),
            "managed_by_service": detail.get("OwningService"),
            "rotation_enabled": bool(detail.get("RotationEnabled")),
        }

    def current_value(self, arn):
        result = self.call(
            self.sm, "get_secret_value", SecretId=arn, VersionStage="AWSCURRENT"
        )
        require(
            result.get("ARN") == arn and isinstance(result.get("VersionId"), str),
            "TARGET_READ_IDENTITY_MISMATCH",
        )
        if isinstance(result.get("SecretString"), str):
            return result["VersionId"], SecretValue(
                result["SecretString"].encode("utf-8"), "string"
            )
        require(isinstance(result.get("SecretBinary"), bytes), "TARGET_VALUE_INVALID")
        return result["VersionId"], SecretValue(result["SecretBinary"], "binary")

    def create(self, name, value, token, tags):
        options = {
            "Name": name,
            "SecretString": value.data.decode("utf-8"),
            "ClientRequestToken": token,
            "Description": DESCRIPTION,
            "Tags": tag_list(tags),
        }
        if self.config.get("kms_key_id"):
            options["KmsKeyId"] = self.config["kms_key_id"]
        result = self.call(self.sm, "create_secret", **options)
        self.check_arn(result.get("ARN"), name)
        require(result.get("VersionId") == token, "TARGET_WRITE_RESPONSE_INVALID")
        return result["ARN"]

    def put(self, arn, value, token):
        result = self.call(
            self.sm,
            "put_secret_value",
            SecretId=arn,
            SecretString=value.data.decode("utf-8"),
            ClientRequestToken=token,
        )
        require(
            result.get("ARN") == arn and result.get("VersionId") == token,
            "TARGET_WRITE_RESPONSE_INVALID",
        )
        return token

    def tag(self, arn, tags):
        self.call(self.sm, "tag_resource", SecretId=arn, Tags=tag_list(tags))
