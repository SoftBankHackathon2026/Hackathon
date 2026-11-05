"""App deletion on Cloud Run, one bounded step per worker tick.

requested -> service_deleting -> [data_deleting] -> deleted

Only the registered service (matching name and UID) is deleted. With data, the app's
Cloud SQL instance (deletion protection is turned off first) and its connection
Secret go too; both must carry this app's ownership labels. This binding updates a
pre-registered service and never creates one, so after deletion an operator has to
prepare the service again before this environment can deploy.
"""

from google.api_core import exceptions as gexc

from controlplane.execution.deletions import Deletion
from controlplane.execution.gcp.journal import JournalTable, read_checkpoint
from controlplane.providers.contracts import ProviderConflict, ProviderUnavailable


def _service(provider):
    try:
        service = provider.services.get_service(
            name=provider.environment.service_name, retry=None, timeout=10
        )
    except gexc.NotFound:
        return None
    except gexc.GoogleAPICallError as exc:
        raise ProviderUnavailable("Cloud Run read failed") from exc
    if service.uid != provider.environment.service_uid:
        raise ProviderConflict("등록된 Cloud Run 서비스가 아닙니다(UID 불일치). 지우지 않았습니다.")
    return service


def step(deletion: Deletion, runtime, store) -> None:
    runtime.connect()
    handlers = {
        "requested": _request_service_deletion,
        "service_deleting": _await_service_deletion,
        "data_deleting": _delete_data_step,
    }
    handler = handlers.get(deletion.state)
    if handler is None:
        raise ValueError("Unknown Cloud Run deletion state")
    handler(deletion, runtime, store)


def _request_service_deletion(deletion, runtime, store):
    provider, env = runtime.provider, runtime.settings.environment
    if _service(provider) is not None:
        try:
            provider.services.delete_service(name=env.service_name, retry=None, timeout=30)
        except gexc.NotFound:
            pass
        except gexc.GoogleAPICallError as exc:
            raise ProviderUnavailable("Cloud Run delete failed") from exc
    deletion.move("service_deleting", f"Cloud Run 서비스 {env.service}를 삭제합니다.")


def _await_service_deletion(deletion, runtime, store):
    if _service(runtime.provider) is not None:
        deletion.wait("Cloud Run 서비스를 삭제하는 중입니다.")
        return
    has_data = _has_data(store, runtime.settings.environment)
    if deletion.delete_data and runtime.data and has_data:
        deletion.move(
            "data_deleting", "Cloud Run 서비스를 삭제했습니다. Cloud SQL과 Secret을 지웁니다."
        )
        return
    deletion.done(
        "Cloud Run 서비스와 모든 리비전을 삭제했습니다."
        + (" Cloud SQL 데이터는 남겨 두었습니다." if has_data else "")
    )


def _delete_data_step(deletion, runtime, store):
    env = runtime.settings.environment
    if _delete_data(deletion, runtime.data):
        with store.connect() as conn:
            conn.execute("DELETE FROM gcp_data WHERE service_name=%s", (env.service_name,))
        deletion.done("Cloud Run 서비스, Cloud SQL 인스턴스, DB 연결 Secret을 삭제했습니다.")


def _has_data(store, env) -> bool:
    return read_checkpoint(store, JournalTable.DATA, env.service_name) is not None


def _delete_data(deletion: Deletion, data) -> bool:
    cp = deletion.cp
    if cp.get("operation") and not _operation_finished(deletion, data):
        return False
    instance = data.api.call_unless_missing("GET", data.instance_url)
    if instance is not None:
        _delete_instance(deletion, data, instance)
        return False
    secret = data.secrets.get(data.secret_url)
    if secret is not None:
        data.owned(secret.get("labels", {}))
        data.api.call_unless_missing("DELETE", data.secret_url)
    return True


def _operation_finished(deletion, data) -> bool:
    cp = deletion.cp
    operation = data.api.call("GET", f"{data.sql}/operations/{cp['operation']}")
    if operation.get("status") != "DONE":
        deletion.wait("Cloud SQL 작업이 끝나기를 기다립니다.")
        return False
    cp.pop("operation")
    if operation.get("error"):
        raise ProviderConflict("Cloud SQL 삭제 작업이 실패했습니다. GCP 작업 이력을 확인하세요.")
    return True


def _delete_instance(deletion, data, instance) -> None:
    cp = deletion.cp
    settings = instance.get("settings", {})
    data.owned(settings.get("userLabels", {}))
    if settings.get("deletionProtectionEnabled"):
        result = data.api.call(
            "PATCH", data.instance_url, body={"settings": {"deletionProtectionEnabled": False}}
        )
        cp["operation"] = result["name"]
        deletion.wait("Cloud SQL 삭제 보호를 해제합니다.")
        return
    if instance.get("state") != "PENDING_DELETE":
        result = data.api.call_unless_missing("DELETE", data.instance_url)
        if result:
            cp["operation"] = result["name"]
    deletion.wait(f"Cloud SQL 인스턴스 {data.instance}를 삭제하는 중입니다.")
