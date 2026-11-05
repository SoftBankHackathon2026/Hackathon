"""Adapter for the common CandidateGateway (same shape as KubernetesCandidateDriver).

prepare registers the release task definition (EcsCheckpoint); submit points
the app's ECS service at it; observe reports the ECS native deployment. Route
initialization (role, target groups, rule, alarm) is a separate worker step.

Restore (Notion "롤백: update_service(이전 revision)"): when a release restores
an earlier one, prepare returns that release's existing task definition
revision instead of registering a new one. Only if the revision is gone
(deregistered) is it registered again from the same request.
"""

from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.providers.contracts import Observation, ProviderConflict, ReleaseKey, Submission
from oneport_aws.candidates import CandidateHandle, Checkpoint, EcsCheckpoint
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider


class OneportEcsCandidateDriver:
    runtime = Runtime.AWS_ECS

    def __init__(self, provider: OneportEcsProvider):
        self.provider = provider
        # release id -> release id it restores; set by the worker from the platform DB.
        # Not part of configuration(): it does not change the infra binding.
        self.restores: dict[str, str] = {}

    def configuration(self) -> dict:
        return {"environment": self.provider.environment.model_dump(mode="json")}

    def prepare(self, request: DeploymentRequest, release: ReleaseKey) -> EcsCheckpoint:
        restore_of = self.restores.get(release.value)
        if restore_of:
            previous = self.provider.find_task_definition(ReleaseKey(value=restore_of))
            if previous:
                return EcsCheckpoint(task_definition_arn=previous)
        return EcsCheckpoint(task_definition_arn=self.provider.register_task(request, release))

    def submit(
        self, request: DeploymentRequest, release: ReleaseKey, checkpoint: Checkpoint
    ) -> Submission:
        if not isinstance(checkpoint, EcsCheckpoint):
            raise TypeError("ECS 체크포인트가 필요합니다.")
        routing = self.provider.initialize(request)  # desired-state; verifies, never moves traffic
        return self.provider.submit(
            request,
            release,
            checkpoint.task_definition_arn,
            routing,
            restore_of=self.restores.get(release.value),
        )

    def observe(self, request: DeploymentRequest, handle: CandidateHandle) -> Observation:
        checkpoint = handle.prepared.checkpoint
        if not isinstance(checkpoint, EcsCheckpoint):
            raise TypeError("ECS 체크포인트가 필요합니다.")
        if handle.submission.resource_id.rsplit("/", 1)[-1] != self.provider.environment.service:
            raise ProviderConflict("다른 앱의 ECS Service입니다.")
        return self.provider.observe(handle.submission.resource_id, checkpoint.task_definition_arn)
