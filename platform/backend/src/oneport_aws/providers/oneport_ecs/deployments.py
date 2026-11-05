from controlplane.providers.contracts import Observation, Phase
from oneport_aws.providers.oneport_ecs.gateways.ecs import (
    ROLLED_BACK,
    SHIFTED_STAGES,
    DeploymentStatus,
    ServiceDeployment,
)

_STOPPED = frozenset({DeploymentStatus.STOPPED, DeploymentStatus.STOP_REQUESTED})


def observe_deployment(deployment: ServiceDeployment, service_arn: str) -> Observation:
    status = deployment.status
    if status == DeploymentStatus.SUCCESSFUL:
        return _observation(Phase.READY, service_arn, "ecs_deployment_completed")
    if status == DeploymentStatus.IN_PROGRESS and deployment.stage in SHIFTED_STAGES:
        return _observation(Phase.READY, service_arn, "ecs_traffic_shifted")
    if status in ROLLED_BACK:
        return _observation(Phase.FAILED, service_arn, "ecs_rolled_back")
    if status in _STOPPED:
        return _observation(Phase.FAILED, service_arn, "ecs_deployment_stopped")
    return _observation(Phase.PENDING, service_arn, "ecs_deployment_in_progress")


def observe_rollback_deployment(
    deployment: ServiceDeployment | None, deployment_arn: str
) -> Observation:
    status = deployment.status if deployment else None
    if status == DeploymentStatus.ROLLBACK_SUCCESSFUL:
        return _observation(Phase.READY, deployment_arn, "ecs_rollback_completed")
    if status == DeploymentStatus.ROLLBACK_FAILED:
        return _observation(Phase.FAILED, deployment_arn, "ecs_rollback_failed")
    return _observation(Phase.PENDING, deployment_arn, "ecs_rollback_pending")


def _observation(phase: Phase, resource_id: str, code: str) -> Observation:
    return Observation(phase=phase, resource_id=resource_id, code=code)
