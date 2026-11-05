import pytest
from pydantic import ValidationError

from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.providers.capabilities import provider_capabilities, validate_provider_contract
from controlplane.providers.gcp.models import GcpDeploymentRequest
from controlplane.providers.interface import DeploymentSpec
from oneport_aws.providers.oneport_ecs.tasks import task_definition_request

BASE = {
    "application": "demo",
    "environment_id": "test",
    "source": {"kind": "image", "image": "docker.io/team/demo@sha256:" + "a" * 64},
    "health_path": "/health",
}


@pytest.mark.parametrize(
    "cpu,memory,units",
    [("0.25", 512, "256"), ("0.25", 2048, "256"), ("0.5", 3072, "512"), ("1", 3072, "1024")],
)
def test_fargate_sizes_compile_into_task_definition(cpu, memory, units):
    request = DeploymentRequest(**BASE, cpu=cpu, memory_mib=memory)
    payload = task_definition_request(
        request,
        execution_role_arn="arn:aws:iam::123456789012:role/execution",
        task_role_arn="arn:aws:iam::123456789012:role/app",
        secret_refs={},
        log_group="/app/demo",
        region="ap-northeast-2",
    )
    assert (payload["cpu"], payload["memory"]) == (units, str(memory))


@pytest.mark.parametrize(
    "cpu,memory", [("0.25", 1536), ("0.25", 3072), ("0.5", 512), ("1", 1024), ("0.1", 256)]
)
def test_fargate_rejects_invalid_pairs_before_render(cpu, memory):
    request = DeploymentRequest(**BASE, cpu=cpu, memory_mib=memory)
    with pytest.raises(ValueError):
        validate_provider_contract(request, Runtime.AWS_ECS)
    with pytest.raises(ValueError):
        task_definition_request(
            request,
            execution_role_arn="unused",
            task_role_arn="unused",
            secret_refs={},
            log_group="unused",
            region="ap-northeast-2",
        )


@pytest.mark.parametrize(
    "cpu,memory", [("0.1", 128), ("0.25", 256), ("0.5", 384), ("0.75", 768), ("1.5", 1536)]
)
def test_small_onprem_values_pass_public_contract(cpu, memory):
    spec = DeploymentSpec(**BASE, cpu=cpu, memory_mib=memory)
    request = DeploymentRequest.model_validate(spec.requested())
    validate_provider_contract(request, Runtime.ONPREM_HELM)


@pytest.mark.parametrize(
    "cpu,memory", [("1", 512), ("1", 768), ("1", 1536), ("2", 1280), ("2", 3840)]
)
def test_cloud_run_accepts_intermediate_memory_sizes(cpu, memory):
    request = GcpDeploymentRequest(**BASE, cpu=cpu, memory_mib=memory)
    assert request.memory_mib == memory


@pytest.mark.parametrize("cpu,memory", [("0.25", 512), ("1", 256), ("2", 768)])
def test_cloud_run_keeps_existing_execution_restrictions(cpu, memory):
    with pytest.raises(ValidationError):
        GcpDeploymentRequest(**BASE, cpu=cpu, memory_mib=memory)


def test_helm_discovery_does_not_offer_more_than_chart_supports():
    caps = provider_capabilities(Runtime.ONPREM_HELM)
    assert max(caps.memory_options_mib) == 2048
    request = DeploymentRequest(**BASE, cpu="1", memory_mib=3072)
    with pytest.raises(ValueError):
        validate_provider_contract(request, Runtime.ONPREM_HELM)
