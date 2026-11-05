import time

from oneport_aws.providers.oneport_ecs.gateways import AwsGateways
from oneport_aws.providers.oneport_ecs.gateways.ecs import family_of
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.routing import COLORS, find_host_rule

DRAIN_POLLS = 60
DRAIN_INTERVAL_SECONDS = 5


def delete_app_resources(gateways: AwsGateways, env: OneportEcsEnvironment) -> list[str]:
    done: list[str] = []
    _delete_service(gateways, env, done)
    _delete_rule(gateways, env, done)
    _delete_target_groups(gateways, env, done)
    _deregister_task_definitions(gateways, env)
    done.append("task_definitions")
    gateways.cloudwatch.delete_alarm(env.alarm_name)
    done.append("alarm")
    _delete_runtime_role(gateways, env, done)
    return done


def _delete_service(gateways: AwsGateways, env: OneportEcsEnvironment, done: list[str]) -> None:
    if gateways.ecs.find_active_service(env.cluster, env.service) is None:
        return
    gateways.ecs.delete_service(env.cluster, env.service)
    done.append("service")
    for _ in range(DRAIN_POLLS):
        status = gateways.ecs.get_service_status(env.cluster, env.service)
        if status is None or status == "INACTIVE":
            return
        time.sleep(DRAIN_INTERVAL_SECONDS)


def _delete_rule(gateways: AwsGateways, env: OneportEcsEnvironment, done: list[str]) -> None:
    rule = find_host_rule(gateways, env)
    if rule:
        gateways.elb.delete_rule(rule.arn)
        done.append("rule")


def _delete_target_groups(
    gateways: AwsGateways, env: OneportEcsEnvironment, done: list[str]
) -> None:
    for color in COLORS:
        group = gateways.elb.find_target_group(env.target_group_name(color))
        if group:
            gateways.elb.delete_target_group(group.arn)
            done.append(f"target_group_{color}")


def _deregister_task_definitions(gateways: AwsGateways, env: OneportEcsEnvironment) -> None:
    families = (env.family, env.migration_family)
    for arn in gateways.ecs.list_task_definitions(env.family):
        if family_of(arn) in families:
            gateways.ecs.deregister_task_definition(arn)


def _delete_runtime_role(
    gateways: AwsGateways, env: OneportEcsEnvironment, done: list[str]
) -> None:
    if gateways.iam.find_role(env.runtime_role_name) is None:
        return
    gateways.iam.detach_policy(env.runtime_role_name, env.runtime_policy_arn)
    gateways.iam.delete_role(env.runtime_role_name)
    done.append("runtime_role")
