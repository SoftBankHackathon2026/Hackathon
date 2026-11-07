"""App deletion on the Helm target, one bounded step per worker tick.

requested -> app_uninstalling -> [data_deleting] -> deleted

The app release goes first (Rollout, Services, Ingress, traffic weights). With data,
the data release is uninstalled and the objects Helm leaves behind are deleted too:
the PostgreSQL PVC and the platform-created credentials Secret. A catalog binding
(an app attached to a shared target) always takes its data with it and is detached
from the app in the same transaction that marks the deletion done; an app left with
no environments is removed from the catalog.
"""

from enum import StrEnum

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from controlplane.providers.contracts import SubmissionUncertain
from controlplane.providers.onprem.helm.kubeapi import read_optional, transient
from controlplane.providers.onprem.helm.plan import release_name

DELETE_TIMEOUT = (3, 15)


class HelmDeletionState(StrEnum):
    REQUESTED = "requested"
    APP_UNINSTALLING = "app_uninstalling"
    DATA_DELETING = "data_deleting"


def _delete(call, name, namespace):
    try:
        call(name, namespace, propagation_policy="Background", _request_timeout=DELETE_TIMEOUT)
    except ApiException as exc:
        if exc.status == 404:
            return
        if transient(exc):
            raise SubmissionUncertain("Kubernetes delete response unavailable") from None
        raise
    except HTTPError:
        raise SubmissionUncertain("Kubernetes delete response unavailable") from None


def _request_app_uninstall(deletion, runtime, app: str) -> None:
    runtime.app.uninstall(app)
    _delete(
        runtime.clients.batch.delete_namespaced_job,
        app + "-helm-test",
        runtime.settings.environment.namespace,
    )
    deletion.move(HelmDeletionState.APP_UNINSTALLING, f"앱 Helm 릴리스 {app}를 삭제합니다.")


def _await_app_uninstall(deletion, runtime, app: str) -> None:
    settings = runtime.settings
    if runtime.app.current(app) is not None:
        deletion.wait("앱 Helm 릴리스를 삭제하는 중입니다.")
    elif deletion.delete_data or settings.catalog_dynamic:
        deletion.move(
            HelmDeletionState.DATA_DELETING, "앱을 삭제했습니다. 데이터 릴리스와 볼륨을 지웁니다."
        )
    else:
        _finish(deletion, settings, "앱을 삭제했습니다. 데이터는 남겨 두었습니다.")


def _delete_data(deletion, runtime, data: str) -> None:
    settings, env, core = runtime.settings, runtime.settings.environment, runtime.clients.core
    runtime.data.uninstall(data)
    leftovers = [
        (
            core.delete_namespaced_persistent_volume_claim,
            core.read_namespaced_persistent_volume_claim,
            "data-" + data + "-postgres-0",
        ),
        (core.delete_namespaced_secret, core.read_namespaced_secret, data + "-credentials"),
    ]
    remaining = False
    for delete, observe, name in leftovers:
        if read_optional(observe, name, env.namespace) is not None:
            _delete(delete, name, env.namespace)
            remaining = True
    if runtime.data.current(data) is not None or remaining:
        deletion.wait("데이터 릴리스와 볼륨을 삭제하는 중입니다.")
        return
    _finish(deletion, settings, "앱과 데이터(DB·Redis·볼륨·접속 정보)를 삭제했습니다.")


def step(deletion, runtime) -> None:
    runtime.connect()
    env = runtime.settings.environment
    app = release_name(env.application, env.environment_id)
    data = release_name(env.application, env.environment_id, data=True)
    state = deletion.state
    if state == HelmDeletionState.REQUESTED:
        _request_app_uninstall(deletion, runtime, app)
    elif state == HelmDeletionState.APP_UNINSTALLING:
        _await_app_uninstall(deletion, runtime, app)
    elif state == HelmDeletionState.DATA_DELETING:
        _delete_data(deletion, runtime, data)
    else:
        raise ValueError("Unknown Helm deletion state")


def _detach_from_catalog(conn, env_id: str, message: str) -> str:
    app = conn.execute(
        "SELECT application_id FROM app_environments WHERE id=%s", (env_id,)
    ).fetchone()
    conn.execute("DELETE FROM app_environments WHERE id=%s", (env_id,))
    siblings = None
    if app:
        siblings = conn.execute(
            "SELECT 1 FROM app_environments WHERE application_id=%s", (app["application_id"],)
        ).fetchone()
    if app and not siblings:
        conn.execute("DELETE FROM applications WHERE id=%s", (app["application_id"],))
        return message + " 남은 배포 환경이 없어 앱 목록에서도 지웠습니다."
    return message + " 이 앱에서 온프렘 환경 연결을 해제했습니다."


def _finish(deletion, settings, message):
    if settings.catalog_dynamic:
        message = _detach_from_catalog(deletion.conn, settings.environment.environment_id, message)
    # done() commits the catalog change together with the deletion state.
    deletion.done(message)
