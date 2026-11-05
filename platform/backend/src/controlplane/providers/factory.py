import json
from pathlib import Path
from typing import Any

from controlplane.domain.models import Runtime
from controlplane.providers.interface import DeploymentProvider
from controlplane.repositories.releases import ReleaseStore


def _native_gcp(payload: dict[str, Any], store: ReleaseStore | None) -> DeploymentProvider:
    from controlplane.execution.gcp.settings import GcpSettings
    from controlplane.providers.gcp.adapter import GcpAdapter

    return GcpAdapter(GcpSettings.model_validate(payload), store=store)


def _bridge_gcp(payload: dict[str, Any], store: ReleaseStore | None) -> DeploymentProvider:
    from controlplane.providers.gcp_bridge.adapter import GcpAdapter
    from controlplane.providers.gcp_bridge.models import GcpSettings

    return GcpAdapter(GcpSettings.model_validate(payload))


def _helm(payload: dict[str, Any], store: ReleaseStore | None) -> DeploymentProvider:
    from controlplane.providers.onprem.helm.adapter import HelmAdapter
    from controlplane.providers.onprem.helm.settings import HelmSettings

    return HelmAdapter(HelmSettings.model_validate(payload), store=store)


def _argo(payload: dict[str, Any], store: ReleaseStore | None) -> DeploymentProvider:
    from controlplane.execution.onprem.settings import ArgoSettings
    from controlplane.providers.onprem.adapter import OnpremAdapter

    return OnpremAdapter(ArgoSettings.model_validate(payload))


def _aws(payload: dict[str, Any], store: ReleaseStore | None) -> DeploymentProvider:
    from oneport_aws.adapter import AwsAdapter
    from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings

    return AwsAdapter(OneportEcsSettings.model_validate(payload), store=store)


def build_adapter(
    runtime: Runtime, payload: dict[str, Any], *, store: ReleaseStore | None = None
) -> DeploymentProvider:
    match runtime:
        case Runtime.ONPREM_ARGO | Runtime.GCP_CLOUD_RUN if payload.get("engine") == "cloud_run":
            return _native_gcp(payload, store)
        case Runtime.ONPREM_HELM:
            return _helm(payload, store)
        case Runtime.ONPREM_ARGO:
            return _argo(payload, store)
        case Runtime.AWS_ECS:
            return _aws(payload, store)
        case Runtime.GCP_CLOUD_RUN:
            return _bridge_gcp(payload, store)
    raise ValueError(f"Unknown runtime {runtime}")


def adapter_from_file(runtime: Runtime, path: Path) -> DeploymentProvider:
    payload = json.loads(path.read_text())
    if runtime == Runtime.AWS_ECS:
        payload["env_file"] = (path.parent / payload["env_file"]).resolve()
    return build_adapter(runtime, payload)
