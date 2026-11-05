from dataclasses import dataclass, field

from kubernetes.client.exceptions import ApiException

from controlplane.providers.contracts import ProviderConflict, ProviderUnavailable
from controlplane.providers.onprem import pods

READ_TIMEOUT = (3, 10)
ROLLOUT_GROUP = ("argoproj.io", "v1alpha1")


@dataclass
class RolloutSnapshot:
    rollout: dict | None = None
    pods: list = field(default_factory=list)


def read_snapshot(clients, env, rollout_name: str) -> RolloutSnapshot:
    try:
        rollout = clients.custom.get_namespaced_custom_object(
            *ROLLOUT_GROUP,
            env.namespace,
            "rollouts",
            rollout_name,
            _request_timeout=READ_TIMEOUT,
        )
    except ApiException as exc:
        if exc.status == 404:
            return RolloutSnapshot()
        raise ProviderUnavailable("앱 상태를 조회하지 못했습니다.") from None
    labels = rollout.get("metadata", {}).get("labels", {})
    if (labels.get(pods.OWNER), labels.get(pods.ENVIRONMENT)) != (
        env.application,
        env.environment_id,
    ):
        raise ProviderConflict("관측 대상 Rollout의 앱·환경 소유권이 다릅니다.")
    selector = rollout["spec"]["selector"]["matchLabels"]
    try:
        items = clients.core.list_namespaced_pod(
            env.namespace,
            label_selector=pods.label_selector(selector),
            _request_timeout=READ_TIMEOUT,
        ).items
    except ApiException:
        raise ProviderUnavailable("앱 Pod 목록을 조회하지 못했습니다.") from None
    return RolloutSnapshot(rollout, items)
