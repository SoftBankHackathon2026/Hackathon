"""Read-only checks of the registered binding. Creates and changes nothing."""

from collections.abc import Callable
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from oneport_aws.providers.oneport_ecs.gateways.base import error_code
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider

UNVERIFIED = (
    "permission boundary/policy ARNs (deployer cannot read IAM policies)",
    "image pull and app health until the first deployment",
)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    observed: str


@dataclass(frozen=True)
class PreflightReport:
    checks: dict[str, CheckResult]
    application_verified: bool = False
    unverified: tuple[str, ...] = UNVERIFIED

    @property
    def ready_for_sdk(self) -> bool:
        return all(check.ok for check in self.checks.values())


def _run(check: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(True, check())
    except ClientError as exc:
        return CheckResult(False, error_code(exc))
    except (BotoCoreError, KeyError, IndexError, ValueError) as exc:
        return CheckResult(False, type(exc).__name__)


def _listener(provider: OneportEcsProvider) -> str:
    env = provider.environment
    found = provider.aws.elbv2.describe_listeners(ListenerArns=[env.listener_arn])["Listeners"][0]
    scheme = "https" if found["Protocol"] == "HTTPS" else "http"
    if scheme != env.endpoint_scheme:
        raise ValueError("listener_scheme_mismatch")
    return f"{found['Protocol']}:{found['Port']}"


def _deployments_api(provider: OneportEcsProvider) -> str:
    env = provider.environment
    try:
        provider.aws.ecs.list_service_deployments(cluster=env.cluster, service=env.service)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ServiceNotFoundException":
            raise
        return "allowed (service not created yet)"
    return "allowed"


def _runtime_role(provider: OneportEcsProvider) -> str:
    env = provider.environment
    try:
        role = provider.aws.iam.get_role(RoleName=env.runtime_role_name)["Role"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchEntity":
            return "not created yet (first deploy creates it)"
        raise
    return role["Arn"]


def _app_rule(provider: OneportEcsProvider) -> str:
    rule = provider.host_rule()
    return rule.priority if rule else "not created yet"


def preflight(provider: OneportEcsProvider) -> PreflightReport:
    env, aws = provider.environment, provider.aws
    checks = {
        "identity": _run(lambda: aws.sts.get_caller_identity()["Arn"].split(":", 5)[-1]),
        "cluster": _run(
            lambda: aws.ecs.describe_clusters(clusters=[env.cluster])["clusters"][0]["status"]
        ),
        "listener": _run(lambda: _listener(provider)),
        "log_group": _run(
            lambda: aws.logs.describe_log_groups(logGroupNamePrefix=env.log_group)["logGroups"][0][
                "logGroupName"
            ]
        ),
        "ecs_service_deployments_api": _run(lambda: _deployments_api(provider)),
        "runtime_role": _run(lambda: _runtime_role(provider)),
        "app_rule": _run(lambda: _app_rule(provider)),
    }
    return PreflightReport(checks)
