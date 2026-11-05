"""Bounded native SDK operations. A durable caller owns intent, polling and HTTP checks.

Kubernetes READY means controller readiness only. Never equate it with application
success or call switch_traffic before the worker verifies candidate HTTP/DB health.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from kubernetes.client import ApiException
from kubernetes.utils.quantity import parse_quantity
from urllib3.exceptions import HTTPError

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import (
    Observation,
    Phase,
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    ReleaseKey,
    Submission,
    SubmissionUncertain,
)
from controlplane.providers.onprem import pods
from controlplane.providers.onprem.bindings import KubernetesBinding
from controlplane.providers.onprem.connection import KubernetesClients

TIMEOUT = (3, 10)
PROVIDER_ID = "onprem_argo"
POD_HOST_FIELDS = ("hostNetwork", "hostPID", "hostIPC", "volumes", "initContainers")
WORKLOAD_KINDS = {"Deployment", "Rollout", "Job"}
IMAGE_ENTRYPOINT_KINDS = {"Deployment", "Rollout"}


class JobOutcome(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def job_outcome(job: dict) -> JobOutcome:
    status = job.get("status", {})
    conditions = {c["type"] for c in status.get("conditions", []) if c.get("status") == "True"}
    if conditions & {"Failed", "FailureTarget"}:
        return JobOutcome.FAILED
    if "Complete" in conditions and status.get("succeeded") == 1:
        return JobOutcome.SUCCEEDED
    return JobOutcome.PENDING


MIGRATION_PHASES = {
    JobOutcome.FAILED: (Phase.FAILED, "migration_failed"),
    JobOutcome.SUCCEEDED: (Phase.READY, "migration_completed"),
    JobOutcome.PENDING: (Phase.PENDING, "migration_pending"),
}


def read_or_none(fetch: Callable[[], Any], subject: str) -> Any:
    try:
        return fetch()
    except ApiException as exc:
        if exc.status == 404:
            return None
        if exc.status == 429 or not exc.status or exc.status >= 500:
            raise ProviderUnavailable(f"{subject} read temporarily unavailable") from None
        raise ProviderRejected(f"{subject} read rejected ({exc.status})") from None
    except (HTTPError, OSError, TimeoutError):
        raise ProviderUnavailable(f"{subject} read temporarily unavailable") from None


def contains(actual, desired, path=()) -> bool:
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            (k not in actual and v in ({}, []))
            or (k in actual and contains(actual[k], v, (*path, k)))
            for k, v in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(desired)
            and all(contains(a, b, path) for a, b in zip(actual, desired, strict=True))
        )
    if len(path) >= 3 and path[-3] == "resources" and path[-2] in {"requests", "limits"}:
        try:
            return parse_quantity(actual) == parse_quantity(desired)
        except (ValueError, TypeError):
            return False
    return actual == desired


def reject_unexpected_pod(pod: dict, kind: str) -> None:
    if any(pod.get(field) for field in POD_HOST_FIELDS):
        raise ProviderConflict("Unexpected privileged or injected workload configuration")
    for container in pod.get("containers", []):
        security = container.get("securityContext", {})
        if (
            security.get("privileged")
            or security.get("capabilities", {}).get("add")
            or container.get("envFrom")
            or container.get("volumeMounts")
            or any(port.get("hostPort") for port in container.get("ports", []))
        ):
            raise ProviderConflict("Unexpected container privileges or injected bindings")
        if kind in IMAGE_ENTRYPOINT_KINDS and (container.get("command") or container.get("args")):
            raise ProviderConflict("Application entrypoint differs from the immutable image")


def reject_unidentified(actual: dict) -> None:
    metadata = actual.get("metadata", {})
    if not metadata.get("uid") or not metadata.get("resourceVersion"):
        raise ProviderConflict("Kubernetes object identity is missing")
    if metadata.get("deletionTimestamp"):
        raise ProviderConflict("Kubernetes object is being deleted")


class KubernetesPrimitives:
    def __init__(self, clients: KubernetesClients, environment: KubernetesBinding):
        self.clients = clients
        self.environment = environment.model_copy(deep=True)

    def _api_for(self, kind: str) -> tuple[Any, str]:
        return {
            "Service": (self.clients.core, "service"),
            "Ingress": (self.clients.networking, "ingress"),
            "Job": (self.clients.batch, "job"),
        }[kind]

    def call(self, method, *, write: bool = False, **kwargs):
        try:
            result = method(_request_timeout=TIMEOUT, **kwargs)
            return self.clients.api.sanitize_for_serialization(result)
        except ApiException as exc:
            if exc.status == 409:
                raise ProviderConflict("Kubernetes object changed or already exists") from None
            if exc.status in {400, 401, 403, 404, 422}:
                raise ProviderRejected(
                    f"Kubernetes rejected the operation ({exc.status})"
                ) from None
            if write:
                raise SubmissionUncertain("Reconcile Kubernetes state before retrying") from None
            raise ProviderUnavailable("Kubernetes read temporarily unavailable") from None
        except (HTTPError, OSError, TimeoutError):
            if write:
                raise SubmissionUncertain("Kubernetes write result is unknown") from None
            raise ProviderUnavailable("Kubernetes read temporarily unavailable") from None

    def read_object(self, kind: str, object_name: str) -> dict | None:
        api, suffix = self._api_for(kind)
        reader = getattr(api, f"read_namespaced_{suffix}")

        def fetch():
            obj = reader(object_name, self.environment.namespace, _request_timeout=TIMEOUT)
            return self.clients.api.sanitize_for_serialization(obj)

        return read_or_none(fetch, "Kubernetes")

    def verify(self, actual: dict | None, desired: dict) -> None:
        if actual is None:
            raise ProviderConflict("Expected Kubernetes resource is missing")
        if desired["kind"] in WORKLOAD_KINDS:
            pod = actual.get("spec", {}).get("template", {}).get("spec", {})
            reject_unexpected_pod(pod, desired["kind"])
        if not contains(actual, desired):
            raise ProviderConflict(
                "Existing object differs from the registered release specification"
            )
        reject_unidentified(actual)

    def ensure(self, desired: dict) -> dict:
        actual = self.read_object(desired["kind"], desired["metadata"]["name"])
        if actual is not None:
            self.verify(actual, desired)
            return actual
        api, suffix = self._api_for(desired["kind"])
        actual = self.call(
            getattr(api, f"create_namespaced_{suffix}"),
            write=True,
            namespace=self.environment.namespace,
            body=desired,
        )
        self.verify(actual, desired)
        return actual

    def check_bindings(self, request: DeploymentRequest) -> dict:
        pods.validate(request, self.environment)
        self.call(
            self.clients.core.read_namespaced_service_account,
            name=self.environment.workload_service_account,
            namespace=self.environment.namespace,
        )
        observed = {}
        for binding in request.secret_bindings:
            ref = self.environment.secret_refs[binding.binding]
            secret = self.call(
                self.clients.core.read_namespaced_secret,
                name=ref.name,
                namespace=self.environment.namespace,
            )
            uid = secret.get("metadata", {}).get("uid")
            if ref.uid is not None and ref.uid != uid:
                raise ProviderConflict("Pinned Secret was deleted or replaced")
            observed[binding.binding] = {"name": ref.name, "key": ref.key, "uid": uid}
            if secret.get("immutable") is not True or ref.key not in secret.get("data", {}):
                raise ProviderRejected("Use an existing immutable Secret with the registered key")
        return observed

    def submit_migration(self, request: DeploymentRequest, release: ReleaseKey) -> Submission:
        self.check_bindings(request)
        job = self.ensure(pods.migration_job(request, self.environment, release))
        return self._submission(job)

    def reconcile_migration(
        self, request: DeploymentRequest, release: ReleaseKey
    ) -> Submission | None:
        desired = pods.migration_job(request, self.environment, release)
        job = self.read_object("Job", desired["metadata"]["name"])
        if job is None:
            return None
        self.verify(job, desired)
        return self._submission(job, recovered=True)

    def observe_migration(
        self, request: DeploymentRequest, release: ReleaseKey, *, expected_uid: str | None = None
    ) -> Observation:
        desired = pods.migration_job(request, self.environment, release)
        job_name = desired["metadata"]["name"]
        job = self.read_object("Job", job_name)
        if job is None:
            return Observation(
                phase=Phase.PENDING, resource_id=job_name, code="migration_not_submitted"
            )
        self.verify(job, desired)
        if expected_uid is not None and job["metadata"]["uid"] != expected_uid:
            raise ProviderConflict("Migration Job was replaced after submission")
        phase, code = MIGRATION_PHASES[job_outcome(job)]
        return Observation(phase=phase, resource_id=job_name, code=code)

    @staticmethod
    def _submission(job: dict, *, recovered: bool = False) -> Submission:
        return Submission(
            provider=PROVIDER_ID,
            resource_id=job["metadata"]["name"],
            operation_id=job["metadata"]["uid"],
            recovered=recovered,
        )
