from copy import deepcopy

from controlplane.core.errors import RequestConflict
from controlplane.domain.models import Runtime
from controlplane.providers.factory import build_adapter


def create_binding(base, application, environment_id, *, hostname=None, owner=None):
    payload = base.settings.model_dump(
        mode="json",
        exclude={
            "database_url",
            "database_url_file",
            "kubeconfig",
            "binary",
            "env_file",
            "credentials_file",
        },
    )
    payload.update(catalog_target_id=None, catalog_dynamic=True)
    if base.runtime == Runtime.AWS_ECS:
        payload.update(
            application=application,
            environment_id=environment_id,
            secret_refs={},
            required_database="none",
        )
    elif base.runtime == Runtime.GCP_CLOUD_RUN:
        payload["catalog_owner"] = owner
        payload["environment"].update(
            application=application,
            environment_id=environment_id,
            service="oneport-" + application,
            service_uid=None,
            service_url=None,
        )
        for policy in ("data_provisioning", "cache_provisioning"):
            if payload.get(policy):
                payload[policy]["app_id"] = application
    elif base.runtime == Runtime.ONPREM_HELM:
        payload.update(demo_database=False, required_database="none", previous_bindings=[])
        payload["environment"].update(
            application=application,
            environment_id=environment_id,
            hostname=hostname,
            secret_refs={},
        )
    else:
        raise RequestConflict("이 환경은 새 앱 생성을 지원하지 않습니다.")
    return payload


def _gcp_identity(settings, store):
    with store.connect() as conn:
        row = conn.execute(
            "SELECT owner,checkpoint FROM gcp_apps WHERE service_name=%s",
            (settings.environment.service_name,),
        ).fetchone()
    if not row:
        return settings
    if row["owner"] != settings.catalog_owner:
        raise RequestConflict("Cloud Run 앱 소유 정보가 변경됐습니다.")
    identity = {
        key: row["checkpoint"][key]
        for key in ("service_uid", "service_url")
        if row["checkpoint"].get(key)
    }
    settings.environment = settings.environment.model_copy(update=identity)
    return settings


def resolve_binding(base, binding, store):
    payload = deepcopy(binding)
    payload["database_url"] = base.settings.database_url
    match base.runtime:
        case Runtime.AWS_ECS:
            payload["env_file"] = base.settings.env_file
            return build_adapter(Runtime.AWS_ECS, payload, store=store)
        case Runtime.GCP_CLOUD_RUN:
            from controlplane.execution.gcp.settings import GcpSettings
            from controlplane.providers.gcp.adapter import GcpAdapter

            payload["credentials_file"] = base.settings.credentials_file
            settings = _gcp_identity(GcpSettings.model_validate(payload), store)
            return GcpAdapter(settings, store=store)
        case Runtime.ONPREM_HELM:
            payload.update(kubeconfig=base.settings.kubeconfig, binary=base.settings.binary)
            return build_adapter(Runtime.ONPREM_HELM, payload, store=store)
    raise RequestConflict("이 환경은 새 앱 생성을 지원하지 않습니다.")
