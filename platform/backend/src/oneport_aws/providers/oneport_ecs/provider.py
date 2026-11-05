import time

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import (
    Observation,
    Phase,
    ProviderConflict,
    ProviderRejected,
    ReleaseKey,
    Submission,
)
from oneport_aws.providers.oneport_ecs.connection import AwsClients
from oneport_aws.providers.oneport_ecs.deployments import (
    observe_deployment,
    observe_rollback_deployment,
)
from oneport_aws.providers.oneport_ecs.gateways import AwsGateways
from oneport_aws.providers.oneport_ecs.gateways.ecs import (
    ROLLED_BACK,
    DeploymentStatus,
    EcsService,
    ServiceDeployment,
    family_of,
)
from oneport_aws.providers.oneport_ecs.gateways.elb import ListenerRule
from oneport_aws.providers.oneport_ecs.images import architectures
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.routing import (
    find_host_rule,
    initialize_routing,
    list_priorities,
)
from oneport_aws.providers.oneport_ecs.service_spec import (
    create_service_params,
    update_service_params,
)
from oneport_aws.providers.oneport_ecs.status import AppStatus, build_status
from oneport_aws.providers.oneport_ecs.task_payload import (
    build_task_payload,
    with_managed_resources,
)
from oneport_aws.providers.oneport_ecs.teardown import delete_app_resources
from oneport_aws.providers.oneport_ecs.validation import validate_deployment

PROVIDER_ID = "aws_ecs"


class OneportEcsProvider:
    def __init__(self, clients: AwsClients, environment: OneportEcsEnvironment):
        if clients.ecs.meta.config.retries.get("total_max_attempts") != 1:
            raise ValueError("ECS SDK client requires Config(retries={'total_max_attempts': 1}).")
        self.aws = clients
        self.gateways = AwsGateways.from_clients(clients)
        self.environment = environment.model_copy(deep=True)

    def validate(self, request: DeploymentRequest) -> None:
        validate_deployment(self.environment, request)

    def allow_public_image(self, image: str) -> None:
        self.environment = self.environment.with_public_image(image)

    def initialize(
        self, request: DeploymentRequest, rule_priority: int | None = None
    ) -> dict[str, str]:
        self.validate(request)
        return initialize_routing(self.gateways, self.environment, request, rule_priority)

    def host_rule(self) -> ListenerRule | None:
        return find_host_rule(self.gateways, self.environment)

    def listener_priorities(self) -> set[int]:
        return list_priorities(self.gateways, self.environment)

    def task_payload(
        self, request: DeploymentRequest, release: ReleaseKey, architecture: str
    ) -> dict:
        return build_task_payload(self.environment, request, release, architecture)

    def find_task_definition(self, release: ReleaseKey, family: str | None = None) -> str | None:
        family = family or self.environment.family
        for arn in self.gateways.ecs.list_recent_task_definitions(family):
            if family_of(arn) == family and self.release_of(arn) == release.value:
                return arn
        return None

    def release_of(self, task_definition_arn: str) -> str | None:
        return self.gateways.ecs.get_task_definition_release(task_definition_arn)

    def register_task(
        self,
        request: DeploymentRequest,
        release: ReleaseKey,
        *,
        retries: int = 6,
        delay: float = 5.0,
    ) -> str:
        self.validate(request)
        existing = self.find_task_definition(release)
        if existing:
            return existing
        available = architectures(
            self.aws.ecr, self.environment, request.source.image, request.architecture
        )
        if available is not None and request.architecture not in available:
            raise ProviderRejected(
                f"이미지에 {request.architecture} 변형이 없습니다 ({sorted(available)})."
            )
        payload = build_task_payload(
            self.environment,
            with_managed_resources(self.environment, request),
            release,
            request.architecture,
        )
        for attempt in range(retries):
            try:
                return self.gateways.ecs.register_task_definition(payload)
            except ProviderRejected:
                if attempt == retries - 1:
                    raise
                time.sleep(delay)
        raise ProviderRejected("Task Definition 등록 재시도 횟수가 0입니다.")

    def describe_service(self) -> EcsService | None:
        env = self.environment
        return self.gateways.ecs.find_active_service(env.cluster, env.service)

    def submit(
        self,
        request: DeploymentRequest,
        release: ReleaseKey,
        task_definition_arn: str,
        routing: dict[str, str],
        restore_of: str | None = None,
    ) -> Submission:
        self.validate(request)
        if self.release_of(task_definition_arn) not in {release.value, restore_of}:
            raise ProviderConflict("다른 Release의 Task Definition입니다.")
        env = self.environment
        service = self.describe_service()
        if service is None:
            params = create_service_params(env, request, release, task_definition_arn, routing)
            arn = self.gateways.ecs.create_service(params)
        elif service.task_definition == task_definition_arn:
            return Submission(provider=PROVIDER_ID, resource_id=service.arn, recovered=True)
        else:
            params = update_service_params(env, request, task_definition_arn)
            arn = self.gateways.ecs.update_service(params)
        return Submission(provider=PROVIDER_ID, resource_id=arn)

    def latest_deployment(self) -> ServiceDeployment | None:
        env = self.environment
        return self.gateways.ecs.find_latest_deployment(env.cluster, env.service)

    def _deployment_of(self, task_definition_arn: str) -> ServiceDeployment | None:
        deployment = self.latest_deployment()
        if deployment is None or deployment.target_revision_arn is None:
            return None
        target = self.gateways.ecs.get_revision_task_definition(deployment.target_revision_arn)
        return deployment if target == task_definition_arn else None

    def observe(self, service_arn: str, task_definition_arn: str) -> Observation:
        deployment = self._deployment_of(task_definition_arn)
        if deployment is None:
            return Observation(
                phase=Phase.PENDING, resource_id=service_arn, code="ecs_deployment_not_observed"
            )
        return observe_deployment(deployment, service_arn)

    def rollback(self, task_definition_arn: str) -> Submission:
        deployment = self._deployment_of(task_definition_arn)
        if deployment is None:
            raise ProviderConflict("되돌릴 배포가 이 Release가 아닙니다.")
        if deployment.status in ROLLED_BACK:
            return Submission(provider=PROVIDER_ID, resource_id=deployment.arn, recovered=True)
        if deployment.status != DeploymentStatus.IN_PROGRESS:
            raise ProviderRejected(
                "bake 시간이 끝나 ECS 롤백을 쓸 수 없습니다. 이전 Release를 재배포하세요."
            )
        self.gateways.ecs.rollback_deployment(deployment.arn)
        return Submission(provider=PROVIDER_ID, resource_id=deployment.arn)

    def observe_rollback(self, deployment_arn: str) -> Observation:
        return observe_rollback_deployment(
            self.gateways.ecs.find_deployment(deployment_arn), deployment_arn
        )

    def status(self) -> AppStatus:
        service = self.describe_service()
        release = self.release_of(service.task_definition) if service else None
        deployment = self.latest_deployment() if service else None
        return build_status(self.gateways, self.environment, service, release, deployment)

    def live_release(self) -> str | None:
        service = self.describe_service()
        return self.release_of(service.task_definition) if service else None

    def delete_app(self) -> list[str]:
        return delete_app_resources(self.gateways, self.environment)
