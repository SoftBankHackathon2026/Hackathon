"""Shared fixtures. Journal tests use a real PostgreSQL schema like the team repo
(skipped when backend/.data/database.json is absent)."""

from pathlib import Path

import pytest

from controlplane.domain.models import DeploymentRequest
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider

from .aws_fakes import ACCOUNT, ALB, LISTENER, REGION, FakeAws

DBFILE = Path(__file__).resolve().parents[1] / ".data/database.json"

NOTION_ENV = f"""# --- 공통 설정값 ---
AWS_ACCESS_KEY_ID=AKIAFAKEFAKEFAKEFAKE
AWS_SECRET_ACCESS_KEY=fake-secret-never-logged
AWS_DEPLOYER_SESSION_NAME=oneport-platform-server
AWS_REGION={REGION}
AWS_ACCOUNT_ID={ACCOUNT}
AWS_DEPLOYER_ROLE_ARN=arn:aws:iam::{ACCOUNT}:role/oneport-deployer
AWS_VPC_ID=vpc-1
AWS_PUBLIC_SUBNET_IDS=subnet-a,subnet-c
AWS_ECS_TASK_SECURITY_GROUP_ID=sg-1
AWS_ECS_CLUSTER_NAME=oneport
AWS_ECS_TASK_EXECUTION_ROLE_ARN=arn:aws:iam::{ACCOUNT}:role/oneport-task-execution
AWS_ECS_INFRASTRUCTURE_ROLE_ARN=arn:aws:iam::{ACCOUNT}:role/oneport-ecs-infrastructure
ONEPORT_RUNTIME_ROLE_PREFIX=oneport-rt-          # task role = oneport-rt-<app-id>
AWS_ECS_LOG_GROUP=/ecs/oneport/apps
AWS_ALB_ARN={ALB}
AWS_ALB_LISTENER_ARN={LISTENER}   # HTTPS :443
ONEPORT_APPS_DOMAIN=app.yubin.dev               # 앱 주소 = <app-id>.app.yubin.dev
ONEPORT_ENDPOINT_SCHEME=https
AWS_ECR_REGISTRY={ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com
ONEPORT_ECR_REPOSITORY_PREFIX=oneport/
ONEPORT_SECRET_PREFIX=oneport/
ONEPORT_DB_INSTANCE_PREFIX=oneport-db-
AWS_RDS_SUBNET_GROUP=oneport-data
AWS_RDS_SECURITY_GROUP_ID=sg-data
AWS_CACHE_SUBNET_IDS=subnet-da,subnet-dc
AWS_CACHE_SECURITY_GROUP_ID=sg-data
ONEPORT_CACHE_PREFIX=oneport-cache-
ONEPORT_CACHE_MAX_STORAGE_GB=1
ONEPORT_CACHE_MAX_ECPU=5000
"""

SECRET_REF = (
    f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:oneport/shop/DATABASE_URL-AbCdEf:::"
    + "a" * 8
    + "-"
    + "b" * 4
    + "-"
    + "c" * 4
    + "-"
    + "d" * 4
    + "-"
    + "e" * 12
)


@pytest.fixture
def env_file(tmp_path) -> Path:
    path = tmp_path / ".env"
    path.write_text(NOTION_ENV)
    return path


@pytest.fixture
def environment(env_file) -> OneportEcsEnvironment:
    from oneport_aws.providers.oneport_ecs.models import environment_from_env_file

    return environment_from_env_file(
        env_file,
        application="shop",
        environment_id="aws-oneport",
        secret_refs={"shop-db": SECRET_REF},
    )


@pytest.fixture
def aws() -> FakeAws:
    return FakeAws()


@pytest.fixture
def provider(aws, environment) -> OneportEcsProvider:
    return OneportEcsProvider(aws, environment)


@pytest.fixture
def make_request(aws, environment):
    def make(**fields) -> DeploymentRequest:
        archs = fields.pop("archs", ("amd64",))
        digest = aws.push(environment.repository, archs)
        image = f"{environment.ecr_registry}/{environment.repository}@{digest}"
        base = {
            "application": "shop",
            "environment_id": "aws-oneport",
            "cpu": "0.5",
            "memory_mib": 1024,
            "source": {"kind": "image", "image": image},
        }
        return DeploymentRequest(**{**base, **fields})

    return make
