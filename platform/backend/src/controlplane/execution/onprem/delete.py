"""App deletion on the Argo Rollouts environment, one bounded step per worker tick.

requested -> resources_deleting -> deleted

The public Ingress goes first so no traffic reaches a half-deleted app, then the
Rollout and its active/preview Services. Only objects carrying this app's labels are
deleted. The next deploy recreates the Services and Rollout from scratch.
"""

from enum import StrEnum

from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.onprem import manifests, pods
from controlplane.providers.onprem.primitives import contains
from controlplane.providers.onprem.provider import GROUP


class ArgoDeletionState(StrEnum):
    REQUESTED = "requested"
    RESOURCES_DELETING = "resources_deleting"


def _objects(provider):
    name = manifests.name(provider.environment)
    return [
        ("Ingress", name),
        ("Service", name + "-active"),
        ("Service", name + "-preview"),
    ]


def _preconditions(obj) -> dict:
    meta = obj["metadata"]
    return {"preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}}


def _delete_routing_objects(provider) -> None:
    env, clients = provider.environment, provider.clients
    labels = pods.labels(env)
    for kind, object_name in _objects(provider):
        obj = provider.read_object(kind, object_name)
        if obj is None:
            continue
        if kind == "Ingress" and not contains(obj["metadata"].get("labels", {}), labels):
            raise ProviderConflict("다른 앱의 Ingress가 있어 지우지 않았습니다.")
        delete = (
            clients.networking.delete_namespaced_ingress
            if kind == "Ingress"
            else clients.core.delete_namespaced_service
        )
        provider.call(
            delete,
            write=True,
            name=object_name,
            namespace=env.namespace,
            body=_preconditions(obj),
        )


def _delete_rollout(provider) -> None:
    env = provider.environment
    rollout = provider.read_rollout()
    if rollout is None:
        return
    if not contains(rollout.get("metadata", {}).get("labels", {}), pods.labels(env)):
        raise ProviderConflict("다른 앱의 Rollout이 있어 지우지 않았습니다.")
    provider.call(
        provider.clients.custom.delete_namespaced_custom_object,
        write=True,
        **GROUP,
        namespace=env.namespace,
        name=manifests.name(env),
        body=_preconditions(rollout),
    )


def step(deletion, runtime) -> None:
    runtime.connect()
    provider = runtime.provider
    state = deletion.state
    if state == ArgoDeletionState.REQUESTED:
        _delete_routing_objects(provider)
        _delete_rollout(provider)
        deletion.move(
            ArgoDeletionState.RESOURCES_DELETING,
            "공개 주소(Ingress), Argo Rollout, Service를 삭제합니다.",
        )
    elif state == ArgoDeletionState.RESOURCES_DELETING:
        remaining = [obj for obj in _objects(provider) if provider.read_object(*obj) is not None]
        if remaining or provider.read_rollout() is not None:
            deletion.wait("Kubernetes가 앱 리소스를 정리하는 중입니다.")
            return
        deletion.done("Argo Rollout, Service 2개, Ingress를 삭제했습니다.")
    else:
        raise ValueError("Unknown Argo deletion state")
