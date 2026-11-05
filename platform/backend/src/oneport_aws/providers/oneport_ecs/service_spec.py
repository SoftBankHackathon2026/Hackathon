from controlplane.domain.models import DeploymentRequest, Strategy
from controlplane.providers.contracts import ReleaseKey
from oneport_aws.providers.oneport_ecs.gateways.base import Payload
from oneport_aws.providers.oneport_ecs.gateways.ecs import RELEASE_TAG
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment

ECS_STRATEGIES = {Strategy.BLUE_GREEN: "BLUE_GREEN", Strategy.CANARY: "CANARY"}


def whole_minutes(seconds: int) -> int:
    return -(-seconds // 60)


def deployment_configuration(env: OneportEcsEnvironment, request: DeploymentRequest) -> Payload:
    rollout = request.rollout
    config: Payload = {
        "strategy": ECS_STRATEGIES[request.strategy],
        "bakeTimeInMinutes": whole_minutes(rollout.bake_seconds) if rollout else env.bake_minutes,
        "deploymentCircuitBreaker": {"enable": True, "rollback": True},
        "alarms": {"alarmNames": [env.alarm_name], "enable": True, "rollback": True},
    }
    if request.strategy == Strategy.CANARY:
        config["canaryConfiguration"] = {
            "canaryPercent": float(rollout.canary_percent) if rollout else env.canary_percent,
            "canaryBakeTimeInMinutes": whole_minutes(rollout.hold_seconds)
            if rollout
            else env.canary_bake_minutes,
        }
    return config


def network_configuration(env: OneportEcsEnvironment) -> Payload:
    return {
        "awsvpcConfiguration": {
            "subnets": env.subnets,
            "securityGroups": [env.security_group],
            "assignPublicIp": "ENABLED" if env.assign_public_ip else "DISABLED",
        }
    }


def create_service_params(
    env: OneportEcsEnvironment,
    request: DeploymentRequest,
    release: ReleaseKey,
    task_definition_arn: str,
    routing: dict[str, str],
) -> Payload:
    return {
        "cluster": env.cluster,
        "serviceName": env.service,
        "taskDefinition": task_definition_arn,
        "desiredCount": request.replicas,
        "launchType": "FARGATE",
        "networkConfiguration": network_configuration(env),
        "deploymentConfiguration": deployment_configuration(env, request),
        "loadBalancers": [
            {
                "targetGroupArn": routing["blue"],
                "containerName": env.container,
                "containerPort": request.port,
                "advancedConfiguration": {
                    "alternateTargetGroupArn": routing["green"],
                    "productionListenerRule": routing["rule"],
                    "roleArn": env.infrastructure_role_arn,
                },
            }
        ],
        "healthCheckGracePeriodSeconds": 60,
        "clientToken": release.value,
        "propagateTags": "SERVICE",
        "tags": [{"key": RELEASE_TAG, "value": release.value}],
    }


def update_service_params(
    env: OneportEcsEnvironment, request: DeploymentRequest, task_definition_arn: str
) -> Payload:
    return {
        "cluster": env.cluster,
        "service": env.service,
        "taskDefinition": task_definition_arn,
        "desiredCount": request.replicas,
        "deploymentConfiguration": deployment_configuration(env, request),
    }
