"""Operator-owned AWS bindings for the oneport stack (infra Notion "config 값").

Never accept these from HTTP users. Same role as KubernetesEnvironment: one
application in one environment, plus per-release ceilings.
"""

import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from controlplane.domain.models import Model

ARN = r"^arn:aws:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:.+$"
# AWS Distro for OpenTelemetry collector, pinned by digest (multi-arch: amd64, arm64).
OTEL_COLLECTOR_IMAGE = (
    "public.ecr.aws/aws-observability/aws-otel-collector:v0.50.0"
    "@sha256:7968fb60db6a2390a47ba6a2df029745638486e285c9b2487da1b722d0855a3e"
)


class OneportEcsEnvironment(Model):
    # Release identity (same fields/patterns as DeploymentRequest)
    application: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")

    # Shared infrastructure from Terraform output platform_config
    region: str
    account_id: str = Field(pattern=r"^[0-9]{12}$")
    cluster: str
    vpc_id: str
    subnets: list[str] = Field(min_length=1)
    security_group: str
    assign_public_ip: bool = True  # no NAT gateway in the oneport VPC
    execution_role_arn: str = Field(pattern=ARN)
    infrastructure_role_arn: str = Field(pattern=ARN)
    runtime_role_prefix: str = "oneport-rt-"
    runtime_app_tag_key: str = "OneportApp"
    runtime_boundary_arn: str = Field(pattern=ARN)
    runtime_policy_arn: str = Field(pattern=ARN)
    log_group: str
    alb_arn: str = Field(pattern=ARN)
    listener_arn: str = Field(pattern=ARN)
    apps_domain: str
    endpoint_scheme: Literal["http", "https"] = "https"
    ecr_registry: str
    public_images: list[str] = Field(default_factory=list)
    ecr_repository_prefix: str = "oneport/"
    secret_prefix: str = "oneport/"
    db_instance_prefix: str = "oneport-db-"
    rds_subnet_group: str = ""
    rds_security_group: str = ""
    cache_prefix: str = "oneport-cache-"
    cache_subnets: list[str] = Field(default_factory=list)
    cache_security_group: str = ""
    cache_max_storage_gb: int = Field(default=1, ge=1)
    cache_max_ecpu: int = Field(default=5000, ge=1000)

    # Tracing (infra output ONEPORT_TRACING). "xray": every task gets an OpenTelemetry
    # collector sidecar that receives OTLP from the app and sends traces to X-Ray.
    tracing: Literal["off", "xray"] = "off"
    trace_sample_ratio: float = Field(default=1.0, ge=0, le=1)
    otel_collector_image: str = OTEL_COLLECTOR_IMAGE
    # Init image with OpenTelemetry Python packages (platform/autoinstrumentation/python).
    # Empty: Python zero-code instrumentation is not offered.
    python_autoinstrumentation_image: str = ""

    # Release policy for ECS native deployments
    bake_minutes: int = Field(default=5, ge=1, le=60)
    canary_percent: float = Field(default=10.0, gt=0, lt=100)
    canary_bake_minutes: int = Field(default=1, ge=0, le=60)
    first_rule_priority: int = Field(default=100, ge=1, le=49000)

    # Binding name -> Secrets Manager ARN pinned to a VersionId (repo rule, see
    # controlplane.providers.aws). Values are never stored here.
    secret_refs: dict[str, str] = Field(default_factory=dict)

    required_database: Literal["none", "postgresql"] = "none"

    # Per-release ceilings. Blue and green run together during bake.
    max_replicas: int = Field(default=2, ge=1, le=5)
    max_cpu: float = Field(default=1, gt=0, le=2)
    max_memory_mib: int = Field(default=2048, ge=512, le=4096)

    # ---- names fixed by the infra doc (deployer may only touch oneport-*) ----
    @property
    def service(self) -> str:
        return self.application

    @property
    def family(self) -> str:
        return f"oneport-{self.application}"

    @property
    def migration_family(self) -> str:
        return f"oneport-{self.application}-migration"

    @property
    def container(self) -> str:
        return "app"

    @property
    def host(self) -> str:
        return f"{self.application}.{self.apps_domain}"

    @property
    def url(self) -> str:
        return f"{self.endpoint_scheme}://{self.host}"

    @property
    def runtime_role_name(self) -> str:
        return f"{self.runtime_role_prefix}{self.application}"

    @property
    def runtime_role_arn(self) -> str:
        return f"arn:aws:iam::{self.account_id}:role/{self.runtime_role_name}"

    @property
    def alarm_name(self) -> str:
        return f"oneport-{self.application}-unhealthy"

    @property
    def load_balancer_dimension(self) -> str:
        return self.alb_arn[self.alb_arn.index("app/") :]

    @property
    def repository(self) -> str:
        return f"{self.ecr_repository_prefix.rstrip('/')}/{self.application}"

    @property
    def repository_uri(self) -> str:
        return f"{self.ecr_registry}/{self.repository}"

    def with_public_image(self, image: str) -> "OneportEcsEnvironment":
        if image in self.public_images:
            return self
        return self.model_copy(update={"public_images": [*self.public_images, image]})

    def secret_name(self, binding: str) -> str:
        return f"{self.secret_prefix}{self.application}/{binding}"

    @property
    def db_instance(self) -> str:
        return f"{self.db_instance_prefix}{self.application}"

    @property
    def cache_name(self) -> str:
        """Cache, user group and app user ID share this name (infra data-access.md §4).
        ElastiCache allows 40 chars and the disabled default user adds `-d`, so names
        over 38 chars keep the prefix and shorten the app id with a hash."""
        name = f"{self.cache_prefix}{self.application}"
        if len(name) <= 38:
            return name
        digest = hashlib.sha1(self.application.encode()).hexdigest()[:6]
        keep = 38 - len(self.cache_prefix) - 1 - len(digest)
        return f"{self.cache_prefix}{self.application[:keep].rstrip('-')}-{digest}"

    def target_group_name(self, color: Literal["blue", "green"]) -> str:
        """AWS limit is 32 chars; shorten with a hash but keep the oneport- prefix."""
        name = f"oneport-{self.application}-{color}"
        if len(name) <= 32:
            return name
        digest = hashlib.sha1(self.application.encode()).hexdigest()[:6]
        keep = 32 - len("oneport-") - 1 - len(digest) - 1 - len(color)
        return f"oneport-{self.application[:keep].rstrip('-')}-{digest}-{color}"


class AwsCredentials(Model):
    """Notion "인증": server = oneport-platform-dev access key (may only call
    sts:AssumeRole); local = AWS_PROFILE (e.g. oneport-deployer role profile).
    Either way every AWS call runs as AWS_DEPLOYER_ROLE_ARN."""

    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    profile: str | None = None
    deployer_role_arn: str = Field(pattern=ARN)
    session_name: str = "oneport-platform-server"

    @model_validator(mode="after")
    def one_source(self):
        has_key = bool(self.access_key_id and self.secret_access_key)
        if has_key == bool(self.profile):
            raise ValueError(
                "Use either AWS_ACCESS_KEY_ID/SECRET or AWS_PROFILE, not both or neither"
            )
        return self


# ---------------------------------------------------------------- .env loader

_KEYS = {
    "AWS_REGION": "region",
    "AWS_ACCOUNT_ID": "account_id",
    "AWS_ECS_CLUSTER_NAME": "cluster",
    "AWS_VPC_ID": "vpc_id",
    "AWS_PUBLIC_SUBNET_IDS": "subnets",
    "AWS_ECS_TASK_SECURITY_GROUP_ID": "security_group",
    "AWS_ECS_TASK_EXECUTION_ROLE_ARN": "execution_role_arn",
    "AWS_ECS_INFRASTRUCTURE_ROLE_ARN": "infrastructure_role_arn",
    "ONEPORT_RUNTIME_ROLE_PREFIX": "runtime_role_prefix",
    "ONEPORT_RUNTIME_APP_TAG_KEY": "runtime_app_tag_key",
    "ONEPORT_RUNTIME_BOUNDARY_ARN": "runtime_boundary_arn",
    "ONEPORT_RUNTIME_POLICY_ARN": "runtime_policy_arn",
    "AWS_ECS_LOG_GROUP": "log_group",
    "AWS_ALB_ARN": "alb_arn",
    "AWS_ALB_LISTENER_ARN": "listener_arn",
    "ONEPORT_APPS_DOMAIN": "apps_domain",
    "ONEPORT_ENDPOINT_SCHEME": "endpoint_scheme",
    "AWS_ECR_REGISTRY": "ecr_registry",
    "ONEPORT_ECR_REPOSITORY_PREFIX": "ecr_repository_prefix",
    "ONEPORT_SECRET_PREFIX": "secret_prefix",
    "ONEPORT_DB_INSTANCE_PREFIX": "db_instance_prefix",
    "AWS_RDS_SUBNET_GROUP": "rds_subnet_group",
    "AWS_RDS_SECURITY_GROUP_ID": "rds_security_group",
    "ONEPORT_CACHE_PREFIX": "cache_prefix",
    "AWS_CACHE_SUBNET_IDS": "cache_subnets",
    "AWS_CACHE_SECURITY_GROUP_ID": "cache_security_group",
    "ONEPORT_CACHE_MAX_STORAGE_GB": "cache_max_storage_gb",
    "ONEPORT_CACHE_MAX_ECPU": "cache_max_ecpu",
    "ONEPORT_TRACING": "tracing",
    "ONEPORT_TRACE_SAMPLE_RATIO": "trace_sample_ratio",
    "ONEPORT_OTEL_COLLECTOR_IMAGE": "otel_collector_image",
    "ONEPORT_PYTHON_AUTOINSTRUMENTATION_IMAGE": "python_autoinstrumentation_image",
}


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; `#` comments, including trailing ones after a space."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip().strip("'\"")
        values[key.strip()] = value
    return values


def environment_from_env_file(path: Path, **app_fields) -> OneportEcsEnvironment:
    """Build the binding from the Notion `.env` block plus app-level fields
    (application, environment_id, secret_refs, ceilings...)."""
    raw = read_env_file(path)
    fields = {name: raw[key] for key, name in _KEYS.items() if raw.get(key)}
    for key in ("subnets", "cache_subnets"):
        if key in fields:
            fields[key] = [s.strip() for s in fields[key].split(",") if s.strip()]
    account = fields.get("account_id", "")
    # Fallback for older .env blocks without these keys: fixed names from infra/aws/runtime.tf.
    fields.setdefault(
        "runtime_boundary_arn", f"arn:aws:iam::{account}:policy/oneport-app-runtime-boundary"
    )
    fields.setdefault("runtime_policy_arn", f"arn:aws:iam::{account}:policy/oneport-app-runtime")
    return OneportEcsEnvironment(**fields, **app_fields)


def credentials_from_env_file(path: Path) -> AwsCredentials:
    raw = read_env_file(path)
    return AwsCredentials(
        access_key_id=raw.get("AWS_ACCESS_KEY_ID") or None,
        secret_access_key=raw.get("AWS_SECRET_ACCESS_KEY") or None,
        profile=raw.get("AWS_PROFILE") or None,
        deployer_role_arn=raw["AWS_DEPLOYER_ROLE_ARN"],
        session_name=raw.get("AWS_DEPLOYER_SESSION_NAME") or "oneport-platform-server",
    )
