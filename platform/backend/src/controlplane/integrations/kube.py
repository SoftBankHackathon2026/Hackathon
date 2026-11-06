from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from controlplane.domain.fleet import (
    ClusterFacts,
    EnvironmentConfig,
    InventoryItem,
    PodFact,
    Workload,
)
from controlplane.integrations.kube_inventory import (
    ClusterObjects,
    ConfigValue,
    IngressSpec,
    ServiceSpec,
    WorkloadSpec,
    build_inventory,
    pods_by_workload,
)
from controlplane.schemas.migrations import AppState

ARGOPROJ = ("argoproj.io", "v1alpha1")
EXTERNAL_SECRETS = ("external-secrets.io", "v1beta1")
APP_HEALTH = {"Healthy", "Progressing", "Degraded", "Suspended", "Missing"}
APP_SYNC = {"Synced", "OutOfSync"}
REQUEST_TIMEOUT = (3.0, 10.0)


def with_request_timeout(api: client.ApiClient) -> client.ApiClient:
    call_api = api.call_api

    def call(*args, **kwargs):
        if kwargs.get("_request_timeout") is None:
            kwargs["_request_timeout"] = REQUEST_TIMEOUT
        return call_api(*args, **kwargs)

    api.call_api = call
    return api


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
        api = client.ApiClient(configuration)
    elif env.kubeconfig is None:
        raise config.ConfigException(f"{env.id}: kubeconfig 나 in_cluster 를 지정해야 해요")
    else:
        api = config.new_client_from_config(config_file=env.kubeconfig, context=env.context)
    return with_request_timeout(api)


class ClusterReader:
    def __init__(self, env: EnvironmentConfig) -> None:
        self._env = env
        api = api_client(env)
        self._serialize = api.sanitize_for_serialization
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

    def _workload_specs(self) -> list[WorkloadSpec] | None:
        try:
            listed = {
                "deployment": self._apps_api.list_deployment_for_all_namespaces().items,
                "statefulset": self._apps_api.list_stateful_set_for_all_namespaces().items,
                "daemonset": self._apps_api.list_daemon_set_for_all_namespaces().items,
            }
        except ApiException:
            return None
        specs = [
            WorkloadSpec.from_k8s(kind, self._serialize(obj))
            for kind, objs in listed.items()
            for obj in objs
        ]
        rollouts = _optional(lambda: self._custom.list_cluster_custom_object(*ARGOPROJ, "rollouts"))
        specs += [
            WorkloadSpec.from_k8s("rollout", obj) for obj in (rollouts or {}).get("items", [])
        ]
        return specs

    def _configmap(self, namespace: str, name: str) -> list[ConfigValue] | None:
        configmap = _optional(lambda: self._core.read_namespaced_config_map(name, namespace))
        if configmap is None:
            return None
        return [ConfigValue(key, str(value)) for key, value in (configmap.data or {}).items()]

    def _configmap_values(
        self, workloads: list[WorkloadSpec]
    ) -> dict[tuple[str, str], list[ConfigValue]]:
        referenced = sorted({(w.namespace, name) for w in workloads for name in w.configmaps})
        found = {}
        for namespace, name in referenced:
            values = self._configmap(namespace, name)
            if values is not None:
                found[(namespace, name)] = values
        return found

    def _pods(self) -> dict[str, list[PodFact]] | None:
        listed = _optional(lambda: self._core.list_pod_for_all_namespaces().items)
        if listed is None:
            return None
        return pods_by_workload([self._serialize(pod) for pod in listed])

    def _inventory(self) -> list[InventoryItem] | None:
        workloads = self._workload_specs()
        if workloads is None:
            return None
        services = _optional(lambda: self._core.list_service_for_all_namespaces().items) or []
        ingresses = _optional(lambda: self._net.list_ingress_for_all_namespaces().items) or []
        objects = ClusterObjects(
            workloads=workloads,
            services=[ServiceSpec.from_k8s(self._serialize(s)) for s in services],
            ingresses=[IngressSpec.from_k8s(self._serialize(i)) for i in ingresses],
            configmaps=self._configmap_values(workloads),
            pods=self._pods(),
        )
        hints = {c.id: c.endpoint_hints for c in self._env.components if c.endpoint_hints}
        return build_inventory(objects, hints)

    def read(self) -> ClusterFacts:
        observed = datetime.now(UTC)
        try:
            return self._read(observed)
        except ApiException as exc:
            return ClusterFacts(observed_at=observed, connected=False, read_status=exc.status)
        except (HTTPError, OSError):
            return ClusterFacts(observed_at=observed, connected=False)

    def _read(self, observed: datetime) -> ClusterFacts:
        namespace = self._env.app_namespace
        labels = self._core.read_namespace(namespace).metadata.labels or {}
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
            inventory=self._inventory(),
        )
