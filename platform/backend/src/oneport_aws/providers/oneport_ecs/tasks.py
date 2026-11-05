import re

from botocore.session import get_session
from botocore.validate import validate_parameters

from controlplane.domain.models import DeploymentRequest, ImageSource
from controlplane.domain.resources import FARGATE_SIZES, validate_resource_pair


def task_definition_request(
    request: DeploymentRequest,
    *,
    execution_role_arn: str,
    task_role_arn: str,
    secret_refs: dict[str, str],
    log_group: str,
    region: str,
) -> dict:
    """Compile a RegisterTaskDefinition request; this function does NOT call AWS.

    Roles/log group are supplied by the trusted environment profile, not web users.
    Git builds must produce an immutable ImageSource before reaching this boundary.
    """
    if not isinstance(request.source, ImageSource):
        raise TypeError("빌드가 완료된 이미지 digest가 필요합니다.")
    validate_resource_pair(request.cpu, request.memory_mib, FARGATE_SIZES)
    cpu = str(int(float(request.cpu) * 1024))
    for binding in request.secret_bindings:
        # ECS Secrets Manager syntax: secret ARN:json-key:version-stage:version-id.
        # An empty stage and immutable version ID avoid AWSCURRENT changing rollback.
        ref = secret_refs[binding.binding]
        parts = ref.split(":")
        if (
            len(parts) != 10
            or parts[2] != "secretsmanager"
            or parts[5] != "secret"
            or parts[8]
            or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", parts[9])
        ):
            raise ValueError("AWS Secret은 Version ID를 고정한 Secrets Manager ARN이 필요합니다.")
    payload = {
        "family": f"lp-{request.application}",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "executionRoleArn": execution_role_arn,
        "taskRoleArn": task_role_arn,
        "cpu": cpu,
        "memory": str(request.memory_mib),
        "runtimePlatform": {
            "cpuArchitecture": request.architecture,
            "operatingSystemFamily": "LINUX",
        },
        "containerDefinitions": [
            {
                "name": "web",
                "image": request.source.image,
                "essential": True,
                "portMappings": [{"containerPort": request.port, "protocol": "tcp"}],
                "environment": [{"name": "PORT", "value": str(request.port)}]
                + [{"name": k, "value": v} for k, v in sorted(request.env.items())],
                "secrets": [
                    {"name": binding.name, "valueFrom": secret_refs[binding.binding]}
                    for binding in request.secret_bindings
                ],
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": region,
                        "awslogs-stream-prefix": request.application,
                    },
                },
            }
        ],
    }
    shape = (
        get_session().get_service_model("ecs").operation_model("RegisterTaskDefinition").input_shape
    )
    validate_parameters(payload, shape)
    return payload
