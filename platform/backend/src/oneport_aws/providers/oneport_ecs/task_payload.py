from controlplane.domain.models import DeploymentRequest, SecretBinding
from controlplane.providers.contracts import ProviderRejected, ReleaseKey
from oneport_aws.providers.oneport_ecs.gateways.ecs import RELEASE_TAG
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.tasks import task_definition_request
from oneport_aws.providers.oneport_ecs.tracing import add_tracing

DB_BINDING = "db"
CACHE_BINDING = "cache"


def build_task_payload(
    env: OneportEcsEnvironment, request: DeploymentRequest, release: ReleaseKey, architecture: str
) -> dict:
    payload = task_definition_request(
        request,
        execution_role_arn=env.execution_role_arn,
        task_role_arn=env.runtime_role_arn,
        secret_refs=env.secret_refs,
        log_group=env.log_group,
        region=env.region,
    )
    payload["family"] = env.family
    payload["runtimePlatform"]["cpuArchitecture"] = architecture
    container = payload["containerDefinitions"][0]
    container["name"] = env.container
    container["environment"].append({"name": "LAUNCHPAD_RELEASE_ID", "value": release.value})
    add_tracing(payload, env, release.value, request)
    payload["tags"] = [{"key": RELEASE_TAG, "value": release.value}]
    return payload


def _inject_secret(
    request: DeploymentRequest, binding: str, env_name: str, available: bool, not_ready: str
) -> DeploymentRequest:
    if not available:
        raise ProviderRejected(not_ready)
    if any(b.binding == binding or b.name == env_name for b in request.secret_bindings):
        return request
    added = SecretBinding(name=env_name, binding=binding)
    return request.model_copy(update={"secret_bindings": [*request.secret_bindings, added]})


def with_database(env: OneportEcsEnvironment, request: DeploymentRequest) -> DeploymentRequest:
    if request.resources.database != "postgresql":
        return request
    return _inject_secret(
        request,
        DB_BINDING,
        "DATABASE_URL",
        DB_BINDING in env.secret_refs,
        "앱 DB가 아직 준비되지 않았습니다 (db_state가 ready가 아님).",
    )


def with_cache(env: OneportEcsEnvironment, request: DeploymentRequest) -> DeploymentRequest:
    if not request.resources.redis:
        return request
    return _inject_secret(
        request,
        CACHE_BINDING,
        "REDIS_URL",
        CACHE_BINDING in env.secret_refs,
        "앱 캐시가 아직 준비되지 않았습니다 (cache_state가 ready가 아님).",
    )


def with_managed_resources(
    env: OneportEcsEnvironment, request: DeploymentRequest
) -> DeploymentRequest:
    return with_cache(env, with_database(env, request))
