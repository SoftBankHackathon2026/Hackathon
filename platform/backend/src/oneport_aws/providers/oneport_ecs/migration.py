"""Release phase migration (infra data-access.md §3 step 9).

The migration runs before ECS sees the new release: a one-off Fargate task from
the `oneport-<app>-migration` task definition, with the same image, role,
network, secrets (DATABASE_URL/REDIS_URL) and log group as the app, and only the
command replaced. A non-zero exit stops the release and leaves the live service
untouched. Logs land in `/ecs/oneport/apps` as `<app>/migration/<task-id>`.

oneport-deployer may RunTask only the migration family (never the app family).
Every write is reconcilable: the task definition carries the release tag and the
task is started with `startedBy=<release>`, so a lost response is found again
instead of being repeated.
"""

from dataclasses import dataclass
from enum import StrEnum

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import ProviderRejected, ReleaseKey
from oneport_aws.providers.oneport_ecs.gateways.ecs import EcsTask
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.service_spec import network_configuration
from oneport_aws.providers.oneport_ecs.task_payload import (
    build_task_payload,
    with_managed_resources,
)

CONTAINER = "migration"
TASK_STATUSES = ("RUNNING", "STOPPED")


class MigrationState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class MigrationStatus:
    state: MigrationState
    detail: str = ""


def _started_by(release: ReleaseKey) -> str:
    return f"oneport-migration-{release.value}"


def payload(provider: OneportEcsProvider, request: DeploymentRequest, release: ReleaseKey) -> dict:
    env = provider.environment
    body = build_task_payload(
        env, with_managed_resources(env, request), release, request.architecture
    )
    body["family"] = env.migration_family
    container = body["containerDefinitions"][0]
    container["name"] = CONTAINER
    container["command"] = list(request.migration.argv)
    container.pop("portMappings", None)
    return body


def register(provider: OneportEcsProvider, request: DeploymentRequest, release: ReleaseKey) -> str:
    existing = provider.find_task_definition(release, family=provider.environment.migration_family)
    if existing:
        return existing
    return provider.gateways.ecs.register_task_definition(
        payload(provider, request, release), label="마이그레이션 "
    )


def find_task(provider: OneportEcsProvider, release: ReleaseKey) -> str | None:
    for status in TASK_STATUSES:
        arns = provider.gateways.ecs.list_task_arns(
            provider.environment.cluster, _started_by(release), status
        )
        if arns:
            return arns[0]
    return None


def run(provider: OneportEcsProvider, task_definition_arn: str, release: ReleaseKey) -> str:
    env = provider.environment
    if not task_definition_arn.rsplit("/", 1)[-1].startswith(env.migration_family + ":"):
        raise ProviderRejected("마이그레이션 family의 Task Definition만 실행할 수 있습니다.")
    return provider.gateways.ecs.run_task(
        {
            "cluster": env.cluster,
            "taskDefinition": task_definition_arn,
            "launchType": "FARGATE",
            "count": 1,
            "startedBy": _started_by(release),
            "networkConfiguration": network_configuration(env),
        }
    )


def status(provider: OneportEcsProvider, task_arn: str) -> MigrationStatus:
    task = provider.gateways.ecs.find_task(provider.environment.cluster, task_arn)
    if task is None:
        return MigrationStatus(MigrationState.RUNNING, "task 조회 대기")
    if task.last_status != "STOPPED":
        return MigrationStatus(MigrationState.RUNNING, task.last_status or "")
    return _stopped_status(task)


def _stopped_status(task: EcsTask) -> MigrationStatus:
    container = next((c for c in task.containers if c.name == CONTAINER), None)
    if container is None and task.containers:
        container = task.containers[0]
    code = container.exit_code if container else None
    if code == 0:
        return MigrationStatus(MigrationState.SUCCEEDED)
    reason = (container.reason if container else None) or task.stopped_reason or "unknown"
    detail = f"exit code {code}, {reason}" if code is not None else reason
    return MigrationStatus(MigrationState.FAILED, detail)
