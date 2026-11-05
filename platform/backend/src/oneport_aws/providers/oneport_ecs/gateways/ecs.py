from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from controlplane.providers.contracts import (
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
)
from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload, error_code

SERVICE_NOT_FOUND = frozenset({"ServiceNotFoundException"})
RELEASE_TAG = "launchpad:release"
_RUN_TASK_TRANSIENT = frozenset({"ThrottlingException", "ServiceUnavailableException"})
_RUN_TASK_REJECTED = frozenset(
    {"AccessDeniedException", "ClientException", "InvalidParameterException"}
)
_RUN_TASK_UNCERTAIN = "마이그레이션 task 시작 결과 불명. 조회 후 판단합니다."


class DeploymentStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESSFUL = "SUCCESSFUL"
    STOPPED = "STOPPED"
    STOP_REQUESTED = "STOP_REQUESTED"
    ROLLBACK_REQUESTED = "ROLLBACK_REQUESTED"
    ROLLBACK_IN_PROGRESS = "ROLLBACK_IN_PROGRESS"
    ROLLBACK_SUCCESSFUL = "ROLLBACK_SUCCESSFUL"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class LifecycleStage(StrEnum):
    POST_PRODUCTION_TRAFFIC_SHIFT = "POST_PRODUCTION_TRAFFIC_SHIFT"
    BAKE_TIME = "BAKE_TIME"
    CLEAN_UP = "CLEAN_UP"


ROLLED_BACK = frozenset(
    {
        DeploymentStatus.ROLLBACK_REQUESTED,
        DeploymentStatus.ROLLBACK_IN_PROGRESS,
        DeploymentStatus.ROLLBACK_SUCCESSFUL,
        DeploymentStatus.ROLLBACK_FAILED,
    }
)
SHIFTED_STAGES = frozenset(
    {
        LifecycleStage.POST_PRODUCTION_TRAFFIC_SHIFT,
        LifecycleStage.BAKE_TIME,
        LifecycleStage.CLEAN_UP,
    }
)


def family_of(task_definition_arn: str) -> str:
    return task_definition_arn.rsplit("/", 1)[-1].rsplit(":", 1)[0]


@dataclass(frozen=True)
class EcsService:
    arn: str
    task_definition: str
    desired: int | None
    running: int | None
    pending: int | None
    rollout_state: str | None
    failed_tasks: int

    @classmethod
    def from_api(cls, raw: Payload) -> "EcsService":
        primary = next((d for d in raw.get("deployments", []) if d.get("status") == "PRIMARY"), {})
        return cls(
            arn=raw["serviceArn"],
            task_definition=raw["taskDefinition"],
            desired=raw.get("desiredCount"),
            running=raw.get("runningCount"),
            pending=raw.get("pendingCount"),
            rollout_state=primary.get("rolloutState"),
            failed_tasks=primary.get("failedTasks", 0),
        )


@dataclass(frozen=True)
class ServiceDeployment:
    arn: str
    status: str | None
    stage: str | None
    reason: str | None
    target_revision_arn: str | None

    @classmethod
    def from_api(cls, raw: Payload) -> "ServiceDeployment":
        return cls(
            arn=raw["serviceDeploymentArn"],
            status=raw.get("status"),
            stage=raw.get("lifecycleStage"),
            reason=raw.get("statusReason"),
            target_revision_arn=raw.get("targetServiceRevision", {}).get("arn"),
        )


@dataclass(frozen=True)
class TaskContainer:
    name: str | None
    exit_code: int | None
    reason: str | None


@dataclass(frozen=True)
class EcsTask:
    last_status: str | None
    stopped_reason: str | None
    containers: tuple[TaskContainer, ...]

    @classmethod
    def from_api(cls, raw: Payload) -> "EcsTask":
        containers = tuple(
            TaskContainer(c.get("name"), c.get("exitCode"), c.get("reason"))
            for c in raw.get("containers", [])
        )
        return cls(raw.get("lastStatus"), raw.get("stoppedReason"), containers)


class EcsGateway(AwsGateway):
    def list_recent_task_definitions(self, family: str) -> list[str]:
        found = self._read(
            "list_task_definitions",
            familyPrefix=family,
            status="ACTIVE",
            sort="DESC",
            maxResults=20,
        )
        return found.get("taskDefinitionArns", [])

    def list_task_definitions(self, family_prefix: str) -> list[str]:
        found = self._read("list_task_definitions", familyPrefix=family_prefix, status="ACTIVE")
        return found.get("taskDefinitionArns", [])

    def get_task_definition_release(self, task_definition_arn: str) -> str | None:
        found = self._read(
            "describe_task_definition", taskDefinition=task_definition_arn, include=["TAGS"]
        )
        return {t["key"]: t["value"] for t in found.get("tags", [])}.get(RELEASE_TAG)

    def register_task_definition(self, payload: Payload, *, label: str = "") -> str:
        result = self._write("register_task_definition", **payload)
        arn = result.get("taskDefinition", {}).get("taskDefinitionArn")
        if not arn:
            raise SubmissionUncertain(f"{label}Task Definition ARN을 확인할 수 없습니다.")
        return arn

    def deregister_task_definition(self, task_definition_arn: str) -> None:
        self._write("deregister_task_definition", taskDefinition=task_definition_arn)

    def find_active_service(self, cluster: str, name: str) -> EcsService | None:
        found = self._read("describe_services", cluster=cluster, services=[name])
        active = [s for s in found.get("services", []) if s.get("status") == "ACTIVE"]
        return EcsService.from_api(active[0]) if active else None

    def get_service_status(self, cluster: str, name: str) -> str | None:
        found = self._read("describe_services", cluster=cluster, services=[name])
        services = found.get("services", [])
        return services[0]["status"] if services else None

    def create_service(self, params: Payload) -> str:
        return self._service_arn(self._write("create_service", **params))

    def update_service(self, params: Payload) -> str:
        return self._service_arn(self._write("update_service", **params))

    @staticmethod
    def _service_arn(result: Payload) -> str:
        arn = result.get("service", {}).get("serviceArn")
        if not arn:
            raise SubmissionUncertain("ECS Service ARN을 확인할 수 없습니다.")
        return arn

    def delete_service(self, cluster: str, name: str) -> None:
        self._write("delete_service", cluster=cluster, service=name, force=True)

    def find_latest_deployment(self, cluster: str, service: str) -> ServiceDeployment | None:
        briefs = self._read_optional(
            "list_service_deployments",
            SERVICE_NOT_FOUND,
            cluster=cluster,
            service=service,
            maxResults=5,
        )
        if not briefs or not briefs.get("serviceDeployments"):
            return None
        newest = max(
            briefs["serviceDeployments"], key=lambda b: b.get("createdAt") or b.get("startedAt")
        )
        return self.find_deployment(newest["serviceDeploymentArn"])

    def find_deployment(self, deployment_arn: str) -> ServiceDeployment | None:
        found = self._read("describe_service_deployments", serviceDeploymentArns=[deployment_arn])
        deployments = found.get("serviceDeployments") or []
        return ServiceDeployment.from_api(deployments[0]) if deployments else None

    def get_revision_task_definition(self, revision_arn: str) -> str | None:
        found = self._read("describe_service_revisions", serviceRevisionArns=[revision_arn])
        revisions = found.get("serviceRevisions", [])
        return revisions[0]["taskDefinition"] if revisions else None

    def rollback_deployment(self, deployment_arn: str) -> None:
        self._write(
            "stop_service_deployment", serviceDeploymentArn=deployment_arn, stopType="ROLLBACK"
        )

    def list_task_arns(self, cluster: str, started_by: str, desired_status: str) -> list[str]:
        found = self._read(
            "list_tasks", cluster=cluster, startedBy=started_by, desiredStatus=desired_status
        )
        return found.get("taskArns", [])

    def find_task(self, cluster: str, task_arn: str) -> EcsTask | None:
        found = self._read("describe_tasks", cluster=cluster, tasks=[task_arn])
        tasks = found.get("tasks") or []
        return EcsTask.from_api(tasks[0]) if tasks else None

    def run_task(self, params: Payload) -> str:
        result = self._invoke_run_task(params)
        failures = result.get("failures") or []
        if failures:
            reason = failures[0].get("reason", "unknown")
            raise ProviderRejected(f"마이그레이션 task를 시작하지 못했습니다 ({reason}).")
        tasks = result.get("tasks") or []
        if not tasks:
            raise SubmissionUncertain("마이그레이션 task 시작 결과를 확인할 수 없습니다.")
        return tasks[0]["taskArn"]

    def _invoke_run_task(self, params: Payload) -> Any:
        try:
            return self._client.run_task(**params)
        except ClientError as exc:
            code = error_code(exc)
            message = exc.response.get("Error", {}).get("Message", "")
            if "unable to assume the role" in message or code in _RUN_TASK_TRANSIENT:
                raise ProviderUnavailable(
                    "마이그레이션 task 시작 대기 (IAM role 전파 등)."
                ) from None
            if code in _RUN_TASK_REJECTED:
                raise ProviderRejected(
                    f"마이그레이션 task를 시작할 수 없습니다: {message[:200]}"
                ) from None
            raise SubmissionUncertain(_RUN_TASK_UNCERTAIN) from None
        except BotoCoreError:
            raise SubmissionUncertain(_RUN_TASK_UNCERTAIN) from None
