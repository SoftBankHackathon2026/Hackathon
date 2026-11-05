from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from controlplane.domain.fleet import ClusterFacts, EnvironmentConfig, Workload
from controlplane.schemas.migrations import AppState

ARGOPROJ = ("argoproj.io", "v1alpha1")
EXTERNAL_SECRETS = ("external-secrets.io", "v1beta1")
APP_HEALTH = {"Healthy", "Progressing", "Degraded", "Suspended", "Missing"}
APP_SYNC = {"Synced", "OutOfSync"}


def _optional[T](read: Callable[[], T]) -> T | None:
    try:
        return read()
    except ApiException:
        return None


def is_default_deny(policy: Any) -> bool:
    spec = policy.spec
    selector = spec.pod_selector
    empty_selector = not (selector and (selector.match_labels or selector.match_expressions))
    return empty_selector and "Ingress" in (spec.policy_types or []) and not spec.ingress


def workload_of(pod: Any) -> Workload:
    pod_ctx = pod.spec.security_context
    containers = pod.spec.containers or []
    owner = (pod.metadata.labels or {}).get("app.kubernetes.io/name") or (
        pod.metadata.labels or {}
    ).get("app")

    def ctx(container: Any, attribute: str) -> Any:
        own = (
            getattr(container.security_context, attribute, None)
            if container.security_context
            else None
        )
        return own if own is not None else getattr(pod_ctx, attribute, None) if pod_ctx else None

    def drops_all(container: Any) -> bool:
        caps = container.security_context.capabilities if container.security_context else None
        return bool(caps and caps.drop and "ALL" in caps.drop)

    return Workload(
        namespace=pod.metadata.namespace,
        name=owner or pod.metadata.name,
        images=[c.image for c in containers],
        run_as_non_root=all(bool(ctx(c, "run_as_non_root")) for c in containers),
        read_only_root=all(bool(ctx(c, "read_only_root_filesystem")) for c in containers),
        drops_all_capabilities=all(drops_all(c) for c in containers),
        privileged=any(bool(ctx(c, "privileged")) for c in containers),
    )


def app_state(item: dict[str, Any]) -> AppState:
    status = item.get("status") or {}
    sync = (status.get("sync") or {}).get("status")
    health = (status.get("health") or {}).get("status")
    return AppState(
        name=item["metadata"]["name"],
        namespace=((item.get("spec") or {}).get("destination") or {}).get("namespace") or "",
        sync=sync if sync in APP_SYNC else "Unknown",
        health=health if health in APP_HEALTH else "Unknown",
        images=list((status.get("summary") or {}).get("images") or []),
        revision=(status.get("sync") or {}).get("revision"),
    )


def wildcard_roles(roles: list[Any]) -> list[str]:
    return [
        f"{role.metadata.namespace}/{role.metadata.name}"
        for role in roles
        if any(
            "*" in (rule.verbs or []) and "*" in (rule.resources or []) for rule in role.rules or []
        )
    ]


def api_client(env: EnvironmentConfig) -> client.ApiClient:
    """환경마다 독립된 API 클라이언트. 전역 기본 설정을 바꾸지 않는다.

    in_cluster 는 플랫폼이 그 클러스터 안에서 돌 때 Pod ServiceAccount 토큰으로 읽는다(EKS 등).
    그 밖에는 kubeconfig 를 쓴다. GKE 는 kubeconfig 의 exec(gke-gcloud-auth-plugin)로 인증한다.
    """
    if env.in_cluster:
        configuration = client.Configuration()
        config.load_incluster_config(client_configuration=configuration)
        return client.ApiClient(configuration)
    return config.new_client_from_config(config_file=env.kubeconfig, context=env.context)


class ClusterReader:
    def __init__(self, env: EnvironmentConfig) -> None:
        self._env = env
        api = api_client(env)
        self._core = client.CoreV1Api(api)
        self._net = client.NetworkingV1Api(api)
        self._rbac = client.RbacAuthorizationV1Api(api)
        self._custom = client.CustomObjectsApi(api)
        self._apps_api = client.AppsV1Api(api)

    def _argocd_flags(self) -> tuple[bool | None, bool | None]:
        cm = _optional(
            lambda: self._core.read_namespaced_config_map("argocd-cm", self._env.argocd_namespace)
        )
        if cm is None:
            return None, None
        data = cm.data or {}
        return data.get("admin.enabled", "true") == "true", data.get(
            "users.anonymous.enabled", "false"
        ) == "true"

    def _external_secrets(self, namespace: str) -> int | None:
        try:
            listed = self._custom.list_namespaced_custom_object(
                *EXTERNAL_SECRETS, namespace, "externalsecrets"
            )
        except ApiException as exc:
            return 0 if exc.status == 404 else None
        return len(listed.get("items", []))

    def _apps(self) -> list[AppState]:
        listed = _optional(
            lambda: self._custom.list_namespaced_custom_object(
                *ARGOPROJ, self._env.argocd_namespace, "applications"
            )
        )
        return [app_state(item) for item in (listed or {}).get("items", [])]

    def _workloads(self, namespace: str) -> list[Workload]:
        pods = self._core.list_namespaced_pod(
            namespace, field_selector="status.phase=Running"
        ).items
        unique = {}
        for pod in pods:
            workload = workload_of(pod)
            unique.setdefault(workload.name, workload)
        return list(unique.values())

    def _readiness(self, workload: str) -> tuple[int, int] | None:
        namespace, kind, name = workload.split("/")
        read = (
            self._apps_api.read_namespaced_deployment_status
            if kind == "deployment"
            else self._apps_api.read_namespaced_stateful_set_status
        )
        try:
            obj = read(name, namespace)
        except ApiException as exc:
            return (0, 0) if exc.status == 404 else None
        return obj.status.ready_replicas or 0, obj.spec.replicas or 0

    def _workload_ready(self) -> dict[str, tuple[int, int]]:
        found = {}
        for component in self._env.components:
            if component.workload and (ready := self._readiness(component.workload)) is not None:
                found[component.workload] = ready
        return found

    def read(self) -> ClusterFacts:
        namespace = self._env.app_namespace
        observed = datetime.now(UTC)
        try:
            labels = self._core.read_namespace(namespace).metadata.labels or {}
        except (ApiException, HTTPError, OSError):
            return ClusterFacts(observed_at=observed, connected=False)
        policies = self._net.list_namespaced_network_policy(namespace).items
        quotas = self._core.list_namespaced_resource_quota(namespace).items
        roles = _optional(lambda: self._rbac.list_namespaced_role(namespace).items)
        admin, anonymous = self._argocd_flags()
        return ClusterFacts(
            observed_at=observed,
            connected=True,
            namespace_labels={namespace: labels},
            default_deny_namespaces={namespace}
            if any(is_default_deny(p) for p in policies)
            else set(),
            network_policy_count={namespace: len(policies)},
            workloads=self._workloads(namespace),
            wildcard_bindings=wildcard_roles(roles) if roles is not None else None,
            argocd_admin_enabled=admin,
            argocd_anonymous_enabled=anonymous,
            external_secrets=self._external_secrets(namespace),
            resource_quota_namespaces={namespace} if quotas else set(),
            apps=self._apps(),
            workload_ready=self._workload_ready(),
        )
