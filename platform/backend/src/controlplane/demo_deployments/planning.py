from typing import Any

from controlplane.core.errors import RequestConflict
from controlplane.demo_deployments.models import PreviewInput
from controlplane.demo_deployments.snapshots import TargetSnapshot
from controlplane.demo_deployments.states import TargetBlocker
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.interface import DeploymentSpec

DATA_RUNTIMES = {"onprem_helm", "gcp_cloud_run"}
DATA_POLICIES = ("data_provisioning", "cache_provisioning")
FAILED_DATA_STATES = {"failed", "uncertain"}


def default_database_resources(runtime: str) -> dict[str, Any]:
    return {
        "database": "postgresql",
        "redis": True,
        "database_mode": "container" if runtime == "onprem_helm" else "dedicated",
    }


def data_configured(environment: dict[str, Any]) -> bool:
    return bool(environment.get("data_configured")) or any(
        (environment.get(policy) or {}).get("configured") for policy in DATA_POLICIES
    )


def pending_data_states(registration: dict[str, Any]) -> list[str]:
    return [
        registration.get(state)
        for state, chosen in (
            ("db_state", registration.get("database") == "postgresql"),
            ("cache_state", registration.get("redis")),
        )
        if chosen and registration.get(state) != "ready"
    ]


class TargetPlanner:
    def __init__(self, service) -> None:
        self.service = service

    @property
    def deployments(self):
        return self.service.deployments

    @property
    def store(self):
        return self.service.store

    def plan(
        self, runtime: str, environment_id: str | None, request: PreviewInput, identity
    ) -> TargetSnapshot:
        target = TargetSnapshot(runtime=runtime, environment_id=environment_id)
        adapter = self.deployments.registry.get(environment_id) if environment_id else None
        blocker = self._adapter_blocker(adapter, runtime)
        if blocker:
            target.blockers.append(blocker)
            return target
        try:
            self._plan_adapter(target, adapter, request, identity)
        except (ValueError, RequestConflict):
            return TargetSnapshot(
                runtime=runtime,
                environment_id=environment_id,
                blockers=[TargetBlocker.DEFAULTS_REJECTED],
            )
        return target

    @staticmethod
    def _adapter_blocker(adapter, runtime: str) -> TargetBlocker | None:
        if not adapter or adapter.runtime != runtime:
            return TargetBlocker.NOT_CONFIGURED
        if (
            not isinstance(adapter, DurableDeploymentProvider)
            or "image" not in adapter.capabilities().source_kinds
        ):
            return TargetBlocker.IMAGE_DEPLOY_UNSUPPORTED
        return None

    def _check_owner(self, environment_id: str, identity) -> None:
        catalog = self.deployments.registry.catalog
        dynamic = catalog and catalog.require_owner(environment_id, identity)
        if (
            identity
            and not dynamic
            and self.deployments.accounts.owners().get(environment_id) not in (None, identity.team)
        ):
            raise RequestConflict("Target belongs to another team")

    def _plan_adapter(self, target, adapter, request: PreviewInput, identity) -> None:
        environment_id = target.environment_id
        self._check_owner(environment_id, identity)
        environment = adapter.public_environment()
        spec = self._build_spec(adapter, environment, request, environment_id)
        waiting = pending_data_states(environment.get("application_registration") or {})
        if waiting:
            target.spec = spec.requested()
            target.blockers.append(
                TargetBlocker.DATA_FAILED
                if any(state in FAILED_DATA_STATES for state in waiting)
                else TargetBlocker.DATA_PREPARING
            )
            return
        self.deployments.plan(spec, identity)
        target.request = adapter._translate(spec).model_dump(mode="json")
        target.owner_team = identity.team if identity else self.service.owner(environment_id)
        with self.store.connect() as conn:
            latest = self.store.latest_release(conn, environment_id)
        if self.store.release_in_flight(latest):
            target.blockers.append(TargetBlocker.RELEASE_IN_PROGRESS)
        target.spec = spec.requested()
        target.binding_fingerprint = adapter.fingerprint()
        target.last_release_id = latest["id"] if latest else None

    def _build_spec(self, adapter, environment, request: PreviewInput, environment_id: str):
        runtime = adapter.runtime
        payload = dict(environment["request_template"])
        if runtime in DATA_RUNTIMES and not data_configured(environment):
            self._default_fresh_data(payload, adapter, environment_id)
        payload.update(application=adapter.application, environment_id=environment_id)
        payload["rollout"] = {**(payload.get("rollout") or {}), "promotion": "automatic"}
        for key in ("port", "health_path"):
            if getattr(request, key) is not None:
                payload[key] = getattr(request, key)
        if runtime in request.overrides:
            payload.update(request.overrides[runtime].model_dump(exclude_none=True))
        payload["architecture"] = "X86_64"
        return DeploymentSpec.model_validate(payload)

    def _default_fresh_data(self, payload, adapter, environment_id: str) -> None:
        with self.store.connect() as conn:
            served = self.store.has_succeeded_release(
                conn, adapter.runtime, adapter.application, environment_id
            )
        if not served:
            payload["resources"] = {
                **(payload.get("resources") or {}),
                **default_database_resources(adapter.runtime),
            }
