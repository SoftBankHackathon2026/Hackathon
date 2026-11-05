"""Observe controller state without mistaking Helm acceptance for health."""

from controlplane.domain.models import Model
from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.onprem import pods
from controlplane.providers.onprem.helm.kubeapi import READ_TIMEOUT, read_optional
from controlplane.providers.onprem.helm.models import HelmPlan
from controlplane.providers.onprem.helm.plan import validate_plan

CONFIG_ERROR_REASONS = {"CreateContainerConfigError", "InvalidImageName"}
RESTART_THRESHOLD = 3
CONFIG_ERROR_MESSAGE = (
    "앱 컨테이너 설정 오류로 시작하지 못하고 있습니다. 이미지·환경변수·Secret 설정을 확인하세요."
)
RESTART_MESSAGE = (
    "앱이 반복 재시작되어 상태 확인에 실패하고 있습니다. 앱 로그와 DB 연결 설정을 확인하세요."
)
MISSING_DATABASE_HINT = (
    " 현재 배포에는 DATABASE_URL이 없습니다. DB가 필요한 앱이면 PostgreSQL을 선택하세요."
)


class RolloutState(Model):
    uid: str
    resource_version: str
    generation: int
    release: str
    ready: bool
    failed: bool
    observed: bool = False
    step_index: int | None = None
    canary_weight: int | None = None
    traffic_uid: str | None = None
    startup_issue: str | None = None


def _verify_ownership(meta: dict, plan: HelmPlan) -> None:
    if (meta.get("name"), meta.get("namespace")) != (plan.release, plan.namespace) or meta.get(
        "deletionTimestamp"
    ):
        raise ProviderConflict("Missing, deleting or foreign Helm Rollout")
    labels, annotations = meta.get("labels", {}), meta.get("annotations", {})
    expected = {
        pods.OWNER: plan.values["application"],
        pods.ENVIRONMENT: plan.values["environment"],
        "app.kubernetes.io/instance": plan.release,
        "app.kubernetes.io/managed-by": "Helm",
    }
    if (
        any(labels.get(k) != v for k, v in expected.items())
        or annotations.get("meta.helm.sh/release-name") != plan.release
        or annotations.get("meta.helm.sh/release-namespace") != plan.namespace
    ):
        raise ProviderConflict("Helm Rollout ownership differs")


def _has_failed(status: dict, observed: bool) -> bool:
    return observed and (
        bool(status.get("abort"))
        or status.get("phase") == "Degraded"
        or any(
            c.get("type") == "Progressing"
            and c.get("status") == "False"
            and c.get("reason") == "ProgressDeadlineExceeded"
            for c in status.get("conditions", [])
        )
    )


def _is_ready(obj: dict, status: dict, plan: HelmPlan, *, observed: bool, failed: bool) -> bool:
    return (
        not failed
        and not status.get("pauseConditions")
        and not obj["spec"].get("paused")
        and observed
        and status.get("phase") == "Healthy"
        and status.get("stableRS") == status.get("currentPodHash")
        and bool(status.get("stableRS"))
        and status.get("readyReplicas", 0) >= plan.values["replicas"]
    )


def observe(obj: dict, plan: HelmPlan, *, expected_uid: str | None = None) -> RolloutState:
    validate_plan(plan)
    meta = obj.get("metadata", {})
    _verify_ownership(meta, plan)
    if expected_uid is not None and meta.get("uid") != expected_uid:
        raise ProviderConflict("Helm Rollout was replaced")
    try:
        template = obj["spec"]["template"]
        release = template["metadata"]["labels"][pods.RELEASE]
        image = template["spec"]["containers"][0]["image"]
        generation = int(meta["generation"])
        if release != plan.values["releaseId"] or image != plan.values["image"]:
            raise ProviderConflict("Helm Rollout template changed")
        if plan.strategy_type not in obj["spec"].get("strategy", {}):
            raise ProviderConflict("Rollout strategy changed")
        status = obj.get("status", {})
        observed = str(status.get("observedGeneration")) == str(generation)
        failed = _has_failed(status, observed)
        return RolloutState(
            uid=meta["uid"],
            resource_version=meta["resourceVersion"],
            generation=generation,
            release=release,
            ready=_is_ready(obj, status, plan, observed=observed, failed=failed),
            failed=failed,
            observed=observed,
            step_index=status.get("currentStepIndex"),
        )
    except (KeyError, ValueError, TypeError):
        raise ProviderConflict("Incomplete Helm Rollout identity") from None


def _pod_owned_by_rollout(clients, plan: HelmPlan, pod, rollout_uid: str) -> bool:
    owner = next(
        (o for o in pod.metadata.owner_references or [] if o.controller and o.kind == "ReplicaSet"),
        None,
    )
    if not owner:
        return False
    replica_set = read_optional(
        clients.apps.read_namespaced_replica_set, owner.name, plan.namespace
    )
    return (
        replica_set is not None
        and replica_set.metadata.uid == owner.uid
        and any(
            o.controller and o.kind == "Rollout" and o.uid == rollout_uid
            for o in replica_set.metadata.owner_references or []
        )
    )


def _container_issue(container, plan: HelmPlan) -> str | None:
    if container.name != "app" or container.ready:
        return None
    waiting = container.state.waiting if container.state else None
    reason = waiting.reason if waiting else None
    if reason in CONFIG_ERROR_REASONS:
        return CONFIG_ERROR_MESSAGE
    if container.restart_count >= RESTART_THRESHOLD:
        message = RESTART_MESSAGE
        if not any(b["env"] == "DATABASE_URL" for b in plan.values["secretBindings"]):
            message += MISSING_DATABASE_HINT
        return message
    return None


def startup_issue(clients, plan, rollout_uid, pod_hash) -> str | None:
    if not pod_hash:
        return None
    selector = pods.label_selector(
        {
            "app.kubernetes.io/instance": plan.release,
            pods.RELEASE: plan.values["releaseId"],
            "rollouts-pod-template-hash": pod_hash,
        }
    )
    candidates = clients.core.list_namespaced_pod(
        plan.namespace, label_selector=selector, _request_timeout=READ_TIMEOUT
    ).items
    for pod in candidates:
        if pod.metadata.deletion_timestamp or not _pod_owned_by_rollout(
            clients, plan, pod, rollout_uid
        ):
            continue
        for container in pod.status.container_statuses or []:
            issue = _container_issue(container, plan)
            if issue:
                return issue
    return None
