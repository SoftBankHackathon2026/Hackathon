from dataclasses import asdict, dataclass, field

from oneport_aws.providers.oneport_ecs.gateways import AwsGateways
from oneport_aws.providers.oneport_ecs.gateways.ecs import EcsService, ServiceDeployment
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.routing import find_host_rule


@dataclass(frozen=True)
class TrafficWeight:
    weight: int
    healthy: int
    targets: int


@dataclass(frozen=True)
class DeploymentBrief:
    status: str | None
    stage: str | None
    reason: str | None


@dataclass(frozen=True)
class AppStatus:
    application: str
    url: str
    exists: bool
    release: str | None = None
    task_definition: str | None = None
    desired: int | None = None
    running: int | None = None
    pending: int | None = None
    rollout_state: str | None = None
    failed_tasks: int = 0
    deployment: DeploymentBrief | None = None
    traffic: dict[str, TrafficWeight] = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = asdict(self)
        if self.exists:
            return data
        return {key: data[key] for key in ("application", "url", "exists")}


def read_traffic(gateways: AwsGateways, env: OneportEcsEnvironment) -> dict[str, TrafficWeight]:
    rule = find_host_rule(gateways, env)
    traffic: dict[str, TrafficWeight] = {}
    for forward in rule.forwards if rule else ():
        states = gateways.elb.list_target_states(forward.target_group_arn)
        traffic[forward.name] = TrafficWeight(
            weight=forward.weight, healthy=states.count("healthy"), targets=len(states)
        )
    return traffic


def build_status(
    gateways: AwsGateways,
    env: OneportEcsEnvironment,
    service: EcsService | None,
    release: str | None,
    deployment: ServiceDeployment | None,
) -> AppStatus:
    if service is None:
        return AppStatus(application=env.application, url=env.url, exists=False)
    brief = (
        DeploymentBrief(deployment.status, deployment.stage, deployment.reason)
        if deployment
        else None
    )
    return AppStatus(
        application=env.application,
        url=env.url,
        exists=True,
        release=release,
        task_definition=service.task_definition.rsplit("/", 1)[-1],
        desired=service.desired,
        running=service.running,
        pending=service.pending,
        rollout_state=service.rollout_state,
        failed_tasks=service.failed_tasks,
        deployment=brief,
        traffic=read_traffic(gateways, env),
    )
