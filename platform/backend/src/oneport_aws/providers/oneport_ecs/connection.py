"""Explicit credentials only: the oneport-platform-dev key assumes oneport-deployer.

Never reads ambient AWS profiles. SDK retries are disabled (repo rule): a
write whose response was lost must be reconciled by reading, not replayed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from botocore.credentials import DeferredRefreshableCredentials
from botocore.session import get_session

from oneport_aws.providers.oneport_ecs.models import AwsCredentials

CONFIG = Config(retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=20)


@dataclass
class AwsClients:
    ecs: Any
    elbv2: Any
    iam: Any
    ecr: Any
    cloudwatch: Any
    logs: Any
    secrets: Any
    rds: Any
    elasticache: Any
    sts: Any
    xray: Any


def connect(credentials: AwsCredentials, region: str) -> AwsClients:
    """Assume the deployer on first use and renew before expiry (1 hour).
    If the base identity already is the deployer (a role profile), use it as is."""
    if credentials.access_key_id:
        base = boto3.Session(
            aws_access_key_id=credentials.access_key_id.get_secret_value(),
            aws_secret_access_key=credentials.secret_access_key.get_secret_value(),
            region_name=region,
        )
    else:
        base = boto3.Session(profile_name=credentials.profile, region_name=region)
    sts = base.client("sts", config=CONFIG)
    role_name = credentials.deployer_role_arn.rsplit("/", 1)[-1]

    def refresh() -> dict:
        if sts.get_caller_identity()["Arn"].split("/")[1:2] == [role_name]:
            frozen = base.get_credentials().get_frozen_credentials()
            expiry = datetime.now(UTC) + timedelta(minutes=30)
            return {
                "access_key": frozen.access_key,
                "secret_key": frozen.secret_key,
                "token": frozen.token,
                "expiry_time": expiry.isoformat(),
            }
        creds = sts.assume_role(
            RoleArn=credentials.deployer_role_arn,
            RoleSessionName=credentials.session_name,
            DurationSeconds=3600,
        )["Credentials"]
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": creds["Expiration"].isoformat(),
        }

    botocore_session = get_session()
    botocore_session._credentials = DeferredRefreshableCredentials(
        refresh_using=refresh, method="sts-assume-role"
    )
    session = boto3.Session(botocore_session=botocore_session, region_name=region)

    def make(name):
        return session.client(name, config=CONFIG)

    return AwsClients(
        ecs=make("ecs"),
        elbv2=make("elbv2"),
        iam=make("iam"),
        ecr=make("ecr"),
        cloudwatch=make("cloudwatch"),
        logs=make("logs"),
        secrets=make("secretsmanager"),
        rds=make("rds"),
        elasticache=make("elasticache"),
        sts=make("sts"),
        xray=make("xray"),
    )
