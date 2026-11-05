import json

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import ProviderConflict
from oneport_aws.providers.oneport_ecs.gateways import AwsGateways
from oneport_aws.providers.oneport_ecs.gateways.cloudwatch import metric_query
from oneport_aws.providers.oneport_ecs.gateways.elb import ListenerRule, target_group_dimension
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment

COLORS = ("blue", "green")
MAX_RULE_PRIORITY = 50000
_ECS_TASKS_TRUST = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


def first_free_priority(first: int, used: set[int]) -> int:
    for priority in range(first, MAX_RULE_PRIORITY + 1):
        if priority not in used:
            return priority
    raise ValueError("사용 가능한 ALB rule priority가 없습니다.")


def elb_tags(env: OneportEcsEnvironment) -> list[dict[str, str]]:
    return [
        {"Key": "launchpad:application", "Value": env.application},
        {"Key": "launchpad:environment", "Value": env.environment_id},
    ]


def find_host_rule(gateways: AwsGateways, env: OneportEcsEnvironment) -> ListenerRule | None:
    for rule in gateways.elb.list_rules(env.listener_arn):
        if env.host in rule.hosts:
            return rule
    return None


def list_priorities(gateways: AwsGateways, env: OneportEcsEnvironment) -> set[int]:
    rules = gateways.elb.list_rules(env.listener_arn)
    return {int(rule.priority) for rule in rules if rule.priority != "default"}


def ensure_runtime_role(gateways: AwsGateways, env: OneportEcsEnvironment) -> None:
    role = gateways.iam.find_role(env.runtime_role_name)
    if role is None:
        gateways.iam.create_role(
            name=env.runtime_role_name,
            trust_policy=_ECS_TASKS_TRUST,
            boundary_arn=env.runtime_boundary_arn,
            description=f"ECS task role for {env.application} (Oneport platform)",
            tags=[{"Key": env.runtime_app_tag_key, "Value": env.application}],
        )
    elif role.tags.get(env.runtime_app_tag_key) != env.application:
        raise ProviderConflict("다른 앱 태그가 붙은 runtime role입니다.")
    attached = gateways.iam.list_attached_policy_arns(env.runtime_role_name)
    if env.runtime_policy_arn not in attached:
        gateways.iam.attach_policy(env.runtime_role_name, env.runtime_policy_arn)


def ensure_target_group(
    gateways: AwsGateways, env: OneportEcsEnvironment, color: str, request: DeploymentRequest
) -> str:
    name = env.target_group_name(color)
    group = gateways.elb.find_target_group(name)
    if group is None:
        return gateways.elb.create_target_group(
            name=name,
            port=request.port,
            vpc_id=env.vpc_id,
            health_path=request.health_path,
            tags=elb_tags(env),
        )
    if group.vpc_id != env.vpc_id or group.target_type != "ip":
        raise ProviderConflict("등록된 VPC/ip 형식과 다른 Target Group입니다.")
    if group.health_check_path != request.health_path:
        gateways.elb.set_health_check_path(group.arn, request.health_path)
    return group.arn


def ensure_rule(
    gateways: AwsGateways,
    env: OneportEcsEnvironment,
    blue: str,
    green: str,
    priority: int | None,
) -> str:
    existing = find_host_rule(gateways, env)
    if existing:
        if existing.target_group_arns != {blue, green}:
            raise ProviderConflict("앱 host rule이 다른 Target Group을 가리킵니다.")
        return existing.arn
    if priority is None:
        priority = first_free_priority(env.first_rule_priority, list_priorities(gateways, env))
    return gateways.elb.create_host_rule(
        listener_arn=env.listener_arn,
        priority=priority,
        host=env.host,
        blue=blue,
        green=green,
        tags=elb_tags(env),
    )


def ensure_alarm(gateways: AwsGateways, env: OneportEcsEnvironment, blue: str, green: str) -> None:
    metrics = [
        metric_query(
            f"tg{i}",
            namespace="AWS/ApplicationELB",
            metric="UnHealthyHostCount",
            dimensions={
                "TargetGroup": target_group_dimension(arn),
                "LoadBalancer": env.load_balancer_dimension,
            },
            period=60,
            stat="Maximum",
            return_data=False,
        )
        for i, arn in enumerate((blue, green))
    ]
    metrics.append({"Id": "unhealthy", "Expression": "MAX([tg0, tg1])", "ReturnData": True})
    gateways.cloudwatch.put_alarm(env.alarm_name, metrics)


def initialize_routing(
    gateways: AwsGateways,
    env: OneportEcsEnvironment,
    request: DeploymentRequest,
    rule_priority: int | None,
) -> dict[str, str]:
    ensure_runtime_role(gateways, env)
    blue, green = (ensure_target_group(gateways, env, color, request) for color in COLORS)
    rule = ensure_rule(gateways, env, blue, green, rule_priority)
    ensure_alarm(gateways, env, blue, green)
    return {"blue": blue, "green": green, "rule": rule}
