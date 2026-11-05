"""SDK-boundary tests against the in-memory fake: infra Notion names, deployer
conditions, ECS native deployment states. No cloud network."""

from unittest.mock import Mock

import pytest
from botocore.exceptions import ReadTimeoutError

from controlplane.domain.models import ImageSource, Strategy
from controlplane.providers.contracts import (
    Phase,
    ProviderConflict,
    ProviderRejected,
    ReleaseKey,
    SubmissionUncertain,
)
from oneport_aws.candidates import CandidateGateway
from oneport_aws.providers.oneport_ecs import OneportEcsCandidateDriver, OneportEcsProvider
from oneport_aws.providers.oneport_ecs.models import credentials_from_env_file, read_env_file

from .aws_fakes import BOUNDARY, POLICY, error

R1, R2 = ReleaseKey(value="1" * 24), ReleaseKey(value="2" * 24)


def deploy(provider, request, release):
    routing = provider.initialize(request)
    td = provider.register_task(request, release, delay=0)
    return td, provider.submit(request, release, td, routing)


# ---------------------------------------------------------------- binding


def test_notion_env_file_is_read_as_is(environment, env_file):
    assert environment.listener_arn.endswith("/def")  # trailing "# HTTPS :443" stripped
    assert environment.runtime_role_prefix == "oneport-rt-"
    assert environment.subnets == ["subnet-a", "subnet-c"]
    assert environment.url == "https://shop.app.yubin.dev"
    # Not in the shared block: derived from infra/aws/runtime.tf names
    assert environment.runtime_boundary_arn == BOUNDARY and environment.runtime_policy_arn == POLICY
    creds = credentials_from_env_file(env_file)
    assert "fake-secret" not in repr(creds)  # SecretStr
    assert read_env_file(env_file)["AWS_ECS_CLUSTER_NAME"] == "oneport"


def test_long_app_names_keep_oneport_prefix(environment):
    long = environment.model_copy(update={"application": "a" * 30})
    name = long.target_group_name("green")
    assert len(name) <= 32 and name.startswith("oneport-") and name.endswith("-green")


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "change, message",
    [
        ({"strategy": Strategy.ROLLING}, "Blue/Green"),
        ({"replicas": 3}, "상한"),
        ({"secret_bindings": [{"name": "API_KEY", "binding": "unknown"}]}, "Secret"),
    ],
)
def test_unsupported_requests_rejected_before_any_call(
    provider, make_request, aws, change, message
):
    with pytest.raises(ValueError, match=message):
        provider.validate(make_request(**change))
    assert not [c for c in aws.calls if not c.startswith("ecr.")]


def test_cache_requests_need_a_ready_cache(provider, make_request):
    from controlplane.providers.contracts import ProviderRejected

    request = make_request(resources={"redis": True})
    provider.validate(request)  # selection itself is valid; readiness is checked at deploy
    provider.initialize(request)
    with pytest.raises(ProviderRejected, match="캐시"):
        provider.register_task(request, ReleaseKey(value="8" * 24), delay=0)


@pytest.mark.parametrize("cpu,memory", [("0.25", 1536), ("0.5", 512), ("0.1", 256)])
def test_invalid_resource_pair_rejected_before_any_aws_call(
    provider, make_request, aws, cpu, memory
):
    with pytest.raises(ValueError, match="CPU/메모리"):
        provider.validate(make_request(cpu=cpu, memory_mib=memory))
    assert not aws.calls


def test_image_must_be_the_apps_ecr_repository(provider, make_request):
    request = make_request()
    other = request.source.image.replace("/oneport/shop@", "/oneport/blog@")
    with pytest.raises(ValueError, match="oneport/shop"):
        provider.validate(
            request.model_copy(
                update={"source": request.source.model_copy(update={"image": other})}
            )
        )


def test_public_hub_image_requires_explicit_registration(provider, make_request, monkeypatch, aws):
    image = "docker.io/team/app@sha256:" + "c" * 64
    request = make_request().model_copy(update={"source": ImageSource(image=image)})
    with pytest.raises(ValueError, match="등록된 공개"):
        provider.validate(request)
    provider.environment.public_images = [image]
    provider.validate(request)
    verify = Mock(return_value={})
    monkeypatch.setattr("oneport_aws.providers.oneport_ecs.images.verify_public_image", verify)
    td, _ = deploy(provider, request, R1)
    payload = aws.ecs.describe_task_definition(taskDefinition=td)["taskDefinition"]
    assert payload["containerDefinitions"][0]["image"] == image
    verify.assert_called_once_with(image, "X86_64")


# ---------------------------------------------------------------- app resources


def test_initialize_creates_resources_with_infra_names(provider, make_request, aws):
    routing = provider.initialize(make_request(health_path="/health"))

    role = aws.roles["oneport-rt-shop"]
    assert role["PermissionsBoundary"]["PermissionsBoundaryArn"] == BOUNDARY
    assert {"Key": "OneportApp", "Value": "shop"} in role["Tags"]
    assert role["policies"] == {POLICY}
    blue = aws.target_groups["oneport-shop-blue"]
    assert blue["TargetType"] == "ip" and blue["HealthCheckPath"] == "/health"
    assert blue["Attributes"]["deregistration_delay.timeout_seconds"] == "30"
    rule = next(r for r in aws.rules if r["Priority"] == "100")
    assert rule["Conditions"][0]["HostHeaderConfig"]["Values"] == ["shop.app.yubin.dev"]
    weights = {
        g["TargetGroupArn"]: g["Weight"]
        for g in rule["Actions"][0]["ForwardConfig"]["TargetGroups"]
    }
    assert weights == {routing["blue"]: 100, routing["green"]: 0}
    alarm = aws.alarms["oneport-shop-unhealthy"]
    watched = [
        m["MetricStat"]["Metric"]["Dimensions"][0]["Value"]
        for m in alarm["Metrics"]
        if "MetricStat" in m
    ]
    assert [w.split("/")[1] for w in watched] == ["oneport-shop-blue", "oneport-shop-green"]

    assert provider.initialize(make_request(health_path="/health")) == routing  # idempotent
    assert len(aws.rules) == 2 and len(aws.target_groups) == 2


def test_foreign_runtime_role_is_not_taken_over(provider, make_request, aws):
    aws.roles["oneport-rt-shop"] = {
        "Arn": "x",
        "Tags": [{"Key": "OneportApp", "Value": "other"}],
        "policies": set(),
    }
    with pytest.raises(ProviderConflict):
        provider.initialize(make_request())


def test_second_app_gets_next_priority(provider, make_request, environment, aws):
    provider.initialize(make_request())
    blog_env = environment.model_copy(update={"application": "blog"})
    blog = OneportEcsProvider(aws, blog_env)
    digest = aws.push(blog_env.repository)
    request = make_request().model_copy(update={"application": "blog"})
    request = request.model_copy(
        update={
            "source": request.source.model_copy(
                update={"image": f"{blog_env.ecr_registry}/{blog_env.repository}@{digest}"}
            )
        }
    )
    blog.initialize(request)
    assert sorted(r["Priority"] for r in aws.rules if r["Priority"] != "default") == ["100", "101"]


# ---------------------------------------------------------------- task definition


def test_task_definition_matches_infra_doc(provider, make_request, aws):
    request = make_request(secret_bindings=[{"name": "DATABASE_URL", "binding": "shop-db"}])
    provider.initialize(request)
    arn = provider.register_task(request, R1, delay=0)
    td = aws.task_definitions[arn]
    container = td["containerDefinitions"][0]

    assert td["family"] == "oneport-shop" and container["name"] == "app"
    assert container["image"] == request.source.image
    assert td["taskRoleArn"].endswith(":role/oneport-rt-shop")
    assert td["executionRoleArn"].endswith(":role/oneport-task-execution")
    assert container["logConfiguration"]["options"]["awslogs-group"] == "/ecs/oneport/apps"
    assert container["logConfiguration"]["options"]["awslogs-stream-prefix"] == "shop"
    assert {"name": "LAUNCHPAD_RELEASE_ID", "value": R1.value} in container["environment"]
    assert container["secrets"][0]["name"] == "DATABASE_URL"
    assert {"key": "launchpad:release", "value": R1.value} in td["tags"]


def test_lost_registration_response_is_reconciled_not_duplicated(provider, make_request, aws):
    request = make_request()
    provider.initialize(request)
    first = provider.register_task(request, R1, delay=0)
    assert provider.register_task(request, R1, delay=0) == first
    assert sum(1 for t in aws.task_definitions.values() if t["family"] == "oneport-shop") == 1


def test_image_checks_before_registration(provider, make_request, aws):
    with pytest.raises(ProviderRejected, match="X86_64"):
        provider.register_task(make_request(archs=("arm64",)), R1, delay=0)
    arm = make_request(archs=("arm64",), architecture="ARM64")
    assert provider.register_task(arm, R2, delay=0)
    missing = make_request()
    image = missing.source.image.rsplit("@", 1)[0] + "@sha256:" + "f" * 64
    with pytest.raises(ProviderRejected, match="digest"):
        provider.register_task(
            missing.model_copy(
                update={"source": missing.source.model_copy(update={"image": image})}
            ),
            R1,
            delay=0,
        )


def test_versionless_secret_ref_is_rejected(provider, make_request, environment):
    unpinned = environment.secret_refs["shop-db"].rsplit(":", 1)[0] + ":"
    p = OneportEcsProvider(
        provider.aws, environment.model_copy(update={"secret_refs": {"shop-db": unpinned}})
    )
    with pytest.raises(ValueError, match="Version ID"):
        p.register_task(
            make_request(secret_bindings=[{"name": "DB", "binding": "shop-db"}]), R1, delay=0
        )


# ---------------------------------------------------------------- service + deployments


def test_first_release_creates_service_with_native_blue_green(provider, make_request, aws):
    _td, submission = deploy(provider, make_request(), R1)
    svc = aws.services["shop"]
    lb = svc["loadBalancers"][0]
    assert submission.provider == "aws_ecs" and submission.resource_id == svc["serviceArn"]
    assert svc["clientToken"] == R1.value and lb["containerName"] == "app"
    assert lb["targetGroupArn"] == aws.target_groups["oneport-shop-blue"]["TargetGroupArn"]
    advanced = lb["advancedConfiguration"]
    assert (
        advanced["alternateTargetGroupArn"]
        == aws.target_groups["oneport-shop-green"]["TargetGroupArn"]
    )
    assert advanced["roleArn"].endswith(":role/oneport-ecs-infrastructure")
    config = svc["deploymentConfiguration"]
    assert config["strategy"] == "BLUE_GREEN" and config["bakeTimeInMinutes"] == 5
    assert config["alarms"] == {
        "alarmNames": ["oneport-shop-unhealthy"],
        "enable": True,
        "rollback": True,
    }
    assert svc["networkConfiguration"]["awsvpcConfiguration"]["assignPublicIp"] == "ENABLED"


def test_next_release_updates_the_same_service(provider, make_request, aws):
    deploy(provider, make_request(), R1)
    td2, _ = deploy(provider, make_request(strategy=Strategy.CANARY), R2)
    svc = aws.services["shop"]
    assert svc["taskDefinition"] == td2 and len(aws.services) == 1
    assert svc["deploymentConfiguration"]["canaryConfiguration"]["canaryPercent"] == 10.0
    routing = provider.initialize(make_request())
    assert provider.submit(
        make_request(), R2, td2, routing
    ).recovered  # re-submit after lost response


def test_observe_follows_ecs_lifecycle(provider, make_request, aws):
    td, sub = deploy(provider, make_request(), R1)
    assert provider.observe(sub.resource_id, td).phase == Phase.PENDING
    aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")
    ready = provider.observe(sub.resource_id, td)
    assert ready.phase == Phase.READY and ready.code == "ecs_traffic_shifted"
    assert ready.application_verified is False
    aws.advance("shop", "ROLLBACK_SUCCESSFUL")
    assert provider.observe(sub.resource_id, td).code == "ecs_rolled_back"


def test_rollback_only_inside_bake(provider, make_request, aws):
    deploy(provider, make_request(), R1)
    aws.advance("shop", "SUCCESSFUL")
    td2, _ = deploy(provider, make_request(), R2)
    aws.advance("shop", "IN_PROGRESS", "BAKE_TIME")
    rollback = provider.rollback(td2)
    assert "ecs.stop_service_deployment" in aws.calls
    aws.advance("shop", "ROLLBACK_SUCCESSFUL")
    assert provider.observe_rollback(rollback.resource_id).phase == Phase.READY
    assert provider.live_release() == R1.value

    td3, _ = deploy(provider, make_request(), ReleaseKey(value="3" * 24))
    aws.advance("shop", "SUCCESSFUL")
    with pytest.raises(ProviderRejected, match="bake"):
        provider.rollback(td3)


def test_write_errors_follow_repo_contract(provider, make_request, aws):
    request = make_request()
    aws.fail["iam.create_role"] = error("AccessDenied")
    with pytest.raises(ProviderRejected):
        provider.initialize(request)
    aws.fail["elbv2.create_rule"] = ReadTimeoutError(endpoint_url="https://elb")
    with pytest.raises(SubmissionUncertain):
        provider.initialize(request)


def test_sdk_retries_must_be_disabled(aws, environment):
    from .aws_fakes import _Client

    aws.ecs = _Client(aws, "ecs", retries=3)
    with pytest.raises(ValueError, match="total_max_attempts"):
        OneportEcsProvider(aws, environment)


def test_gateway_round_trip(provider, make_request, aws):
    gateway = CandidateGateway({("shop", "aws-oneport"): OneportEcsCandidateDriver(provider)})
    request = make_request()
    provider.initialize(request)
    prepared = gateway.prepare(request, R1)
    handle = gateway.submit(request, prepared)
    assert gateway.observe(request, handle).phase == Phase.PENDING
    aws.advance("shop", "SUCCESSFUL")
    assert gateway.observe(request, handle).code == "ecs_deployment_completed"


def test_delete_app_in_infra_order(provider, make_request, aws):
    deploy(provider, make_request(), R1)
    done = provider.delete_app()
    assert done == [
        "service",
        "rule",
        "target_group_blue",
        "target_group_green",
        "task_definitions",
        "alarm",
        "runtime_role",
    ]
    assert aws.target_groups == {} and aws.alarms == {} and aws.roles == {}
    assert len(aws.rules) == 1  # only the listener default
