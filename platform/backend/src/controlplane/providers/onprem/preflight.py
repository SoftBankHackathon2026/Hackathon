"""Use the platform identity, never require cluster-admin or read system Secrets."""

from enum import StrEnum
from typing import Literal

from controlplane.domain.models import DeploymentRequest, Model
from controlplane.providers.onprem import pods
from controlplane.providers.onprem.provider import GROUP, ArgoProvider

RESOURCE_GRANTS = (
    ("argoproj.io", "rollouts", ("get", "list", "create", "update")),
    ("argoproj.io", "rollouts/status", ("patch",)),
    ("apps", "replicasets", ("list",)),
    ("", "services", ("get", "create")),
    ("networking.k8s.io", "ingresses", ("get", "create", "delete")),
)
JOB_GRANT = ("batch", "jobs", ("get", "create"))
UNVERIFIED = [
    "Argo controller reconciliation",
    "private preview DNS from Worker",
    "node image pull/capacity",
    "public Tunnel HTTPS",
    "DB read/write",
]


class PreflightCode(StrEnum):
    RBAC_DENIED = "rbac_denied"
    REGISTRY_SECRET_TYPE_INVALID = "registry_secret_type_invalid"
    BINDINGS_PRESENT = "bindings_present"


class PermissionCheck(Model):
    namespace: str
    group: str
    resource: str
    verb: str
    name: str | None = None
    allowed: bool


class PreflightReport(Model):
    ready_for_sdk: bool
    application_verified: Literal[False] = False
    permissions: list[PermissionCheck]
    code: PreflightCode
    observed_secret_bindings: dict | None = None
    unverified: list[str] | None = None

    def to_json(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)


def access_review_body(attributes: dict) -> dict:
    return {
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": attributes},
    }


def permissions(provider, request: DeploymentRequest, *, preview_job: bool = False) -> list[dict]:
    env = provider.environment
    grants = list(RESOURCE_GRANTS)
    if request.migration or preview_job:
        grants.append(JOB_GRANT)
    rules = [
        {"namespace": env.namespace, "group": group, "resource": resource, "verb": verb}
        for group, resource, verbs in grants
        for verb in verbs
    ]
    rules.append(
        {
            "namespace": env.namespace,
            "group": "",
            "resource": "serviceaccounts",
            "name": env.workload_service_account,
            "verb": "get",
        }
    )
    secret_names = {
        *env.image_pull_secrets,
        *(env.secret_refs[b.binding].name for b in request.secret_bindings),
    }
    rules.extend(
        {"namespace": env.namespace, "group": "", "resource": "secrets", "name": n, "verb": "get"}
        for n in sorted(secret_names)
    )
    return rules


def check_permissions(
    provider: ArgoProvider, request: DeploymentRequest, *, preview_job: bool
) -> list[PermissionCheck]:
    checks = []
    for attributes in permissions(provider, request, preview_job=preview_job):
        review = provider.call(
            provider.clients.authorization.create_self_subject_access_review,
            body=access_review_body(attributes),
        )
        allowed = review.get("status", {}).get("allowed") is True
        checks.append(PermissionCheck(**attributes, allowed=allowed))
    return checks


def preflight(
    provider: ArgoProvider, request: DeploymentRequest, *, preview_job: bool = False
) -> PreflightReport:
    pods.validate(request, provider.environment)
    checks = check_permissions(provider, request, preview_job=preview_job)
    if not all(c.allowed for c in checks):
        return PreflightReport(
            ready_for_sdk=False, permissions=checks, code=PreflightCode.RBAC_DENIED
        )
    provider.call(
        provider.clients.custom.list_namespaced_custom_object,
        **GROUP,
        namespace=provider.environment.namespace,
        limit=1,
    )
    bindings = provider.check_bindings(request)
    for secret_name in provider.environment.image_pull_secrets:
        secret = provider.call(
            provider.clients.core.read_namespaced_secret,
            namespace=provider.environment.namespace,
            name=secret_name,
        )
        if secret.get("type") != "kubernetes.io/dockerconfigjson":
            return PreflightReport(
                ready_for_sdk=False,
                permissions=checks,
                code=PreflightCode.REGISTRY_SECRET_TYPE_INVALID,
            )
    return PreflightReport(
        ready_for_sdk=True,
        permissions=checks,
        observed_secret_bindings=bindings,
        code=PreflightCode.BINDINGS_PRESENT,
        unverified=UNVERIFIED,
    )
