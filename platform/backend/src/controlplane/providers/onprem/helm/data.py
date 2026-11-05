"""App-owned credentials and data identities. Never return secret values to the journal."""

import base64
import secrets
from urllib.parse import urlsplit

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from controlplane.providers.contracts import ProviderConflict, SubmissionUncertain
from controlplane.providers.onprem.helm.kubeapi import READ_TIMEOUT, read_optional
from controlplane.providers.onprem.helm.models import HelmPlan
from controlplane.providers.onprem.helm.plan import fingerprint, release_name

DATA_NAMESPACE = "oneport-apps"
CREDENTIAL_INTENT = "oneport.io/credential-intent"
CLUSTER_SUFFIX = "svc.cluster.local"
POSTGRES_PORT = 5432
REDIS_PORT = 6379
DATA_KINDS_WITH_REPLICAS = {"StatefulSet", "Deployment"}


def credential_labels(env, release: str) -> dict:
    return {
        "launchpad.io/application": env.application,
        "launchpad.io/environment": env.environment_id,
        "app.kubernetes.io/instance": release,
        "app.kubernetes.io/component": "credentials",
        "app.kubernetes.io/managed-by": "oneport-platform",
    }


def _postgres_host(release: str, namespace: str) -> str:
    return f"{release}-postgres.{namespace}.{CLUSTER_SUFFIX}"


def _redis_host(release: str, namespace: str) -> str:
    return f"{release}-redis.{namespace}.{CLUSTER_SUFFIX}"


def _new_credential_values(release: str, namespace: str) -> dict:
    admin, password, redis = [secrets.token_hex(24) for _ in range(3)]
    return {
        "postgres-admin-password": admin,
        "postgres-app-password": password,
        "redis-password": redis,
        "DATABASE_URL": f"postgresql://app:{password}@{_postgres_host(release, namespace)}:{POSTGRES_PORT}/app",
        "REDIS_URL": f"redis://:{redis}@{_redis_host(release, namespace)}:{REDIS_PORT}/0",
    }


def _reject_orphaned_data(clients, release: str, namespace: str) -> None:
    checks = [
        (clients.core.read_namespaced_persistent_volume_claim, "data-" + release + "-postgres-0"),
        (clients.apps.read_namespaced_stateful_set, release + "-postgres"),
        (clients.apps.read_namespaced_deployment, release + "-redis"),
    ]
    if any(read_optional(call, resource, namespace) is not None for call, resource in checks):
        raise ProviderConflict("Data remains without recorded credentials; recovery required")


def _create_credentials(
    clients, name: str, labels: dict, release: str, intent: str, namespace: str
):
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "immutable": True,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "annotations": {CREDENTIAL_INTENT: intent},
        },
        "stringData": _new_credential_values(release, namespace),
    }
    uncertain = "Credential creation response unavailable; reconcile saved intent"
    try:
        return clients.core.create_namespaced_secret(namespace, body, _request_timeout=READ_TIMEOUT)
    except ApiException as exc:
        if exc.status == 409 or exc.status >= 500:
            raise SubmissionUncertain(uncertain) from None
        raise
    except HTTPError:
        raise SubmissionUncertain(uncertain) from None


def _verify_credentials(existing, labels: dict, intent: str, expected: dict | None) -> None:
    if expected and existing.metadata.uid != expected["credentialsSecretUid"]:
        raise ProviderConflict("Data credential UID changed")
    if not expected and (existing.metadata.annotations or {}).get(CREDENTIAL_INTENT) != intent:
        raise ProviderConflict("Unrecorded credentials belong to a different creation intent")
    if (
        existing.type != "Opaque"
        or existing.immutable is not True
        or any((existing.metadata.labels or {}).get(k) != v for k, v in labels.items())
    ):
        raise ProviderConflict("Credential ownership or immutability differs")


def _verify_connection_binding(existing, release: str, namespace: str) -> None:
    try:
        values = {key: base64.b64decode(value).decode() for key, value in existing.data.items()}
        pg, cache = urlsplit(values["DATABASE_URL"]), urlsplit(values["REDIS_URL"])
        postgres_matches = (pg.scheme, pg.hostname, pg.port, pg.username, pg.password, pg.path) == (
            "postgresql",
            _postgres_host(release, namespace),
            POSTGRES_PORT,
            "app",
            values["postgres-app-password"],
            "/app",
        )
        redis_matches = (cache.hostname, cache.port, cache.password, cache.path) == (
            _redis_host(release, namespace),
            REDIS_PORT,
            values["redis-password"],
            "/0",
        )
        consistent = postgres_matches and redis_matches and bool(values["postgres-admin-password"])
    except (KeyError, ValueError, UnicodeError):
        consistent = False
    if not consistent:
        raise ProviderConflict("Data connection binding is inconsistent")


def prepare(clients, env, request, intent, expected=None):
    release = release_name(env.application, env.environment_id, data=True)
    name, namespace = release + "-credentials", env.namespace
    labels = credential_labels(env, release)
    existing = read_optional(clients.core.read_namespaced_secret, name, namespace)
    if existing is None:
        if expected:
            raise ProviderConflict("Recorded data credentials are missing; recovery required")
        _reject_orphaned_data(clients, release, namespace)
        existing = _create_credentials(clients, name, labels, release, intent, namespace)
    _verify_credentials(existing, labels, intent, expected)
    _verify_connection_binding(existing, release, namespace)
    result = {
        "application": env.application,
        "environment": env.environment_id,
        "credentialsSecret": name,
        "credentialsSecretUid": existing.metadata.uid,
        "postgresql": {
            "enabled": request.resources.database == "postgresql",
            "database": "app",
            "username": "app",
            "storageGi": 1,
        },
        "redis": {"enabled": request.resources.redis},
    }
    if expected and result != expected:
        raise ProviderConflict("Data settings changed; explicit migration required")
    return result


def plan(values, artifact):
    identity = {
        "release": release_name(values["application"], values["environment"], data=True),
        "namespace": DATA_NAMESPACE,
        "chart_sha256": artifact.sha256,
        "values": values,
    }
    return HelmPlan(**identity, fingerprint=fingerprint(identity))


def _data_resources(clients, plan: HelmPlan) -> list[tuple[str, object, str]]:
    values, release = plan.values, plan.release
    items = [
        ("Service", clients.core.read_namespaced_service, release + "-" + suffix)
        for suffix, enabled in [
            ("postgres", values["postgresql"]["enabled"]),
            ("redis", values["redis"]["enabled"]),
        ]
        if enabled
    ]
    if values["postgresql"]["enabled"]:
        items += [
            ("StatefulSet", clients.apps.read_namespaced_stateful_set, release + "-postgres"),
            (
                "PersistentVolumeClaim",
                clients.core.read_namespaced_persistent_volume_claim,
                "data-" + release + "-postgres-0",
            ),
        ]
    if values["redis"]["enabled"]:
        items += [("Deployment", clients.apps.read_namespaced_deployment, release + "-redis")]
    return items


def _resource_is_ready(kind: str, obj) -> bool:
    if kind in DATA_KINDS_WITH_REPLICAS:
        return (
            obj.status.observed_generation == obj.metadata.generation
            and (obj.status.ready_replicas or 0) == 1
        )
    if kind == "PersistentVolumeClaim":
        return obj.status.phase == "Bound"
    return True


def observe(clients, plan, known):
    values = plan.values
    ready, identities = True, dict(known)
    for kind, call, name in _data_resources(clients, plan):
        obj = read_optional(call, name, plan.namespace)
        key = kind + "/" + name
        if obj is None:
            if key in identities:
                raise ProviderConflict("Recorded data resource disappeared")
            ready = False
            continue
        if key in identities and identities[key] != obj.metadata.uid:
            raise ProviderConflict("Data resource UID changed")
        labels = obj.metadata.labels or {}
        if any(
            labels.get("launchpad.io/" + k) != values[k] for k in ("application", "environment")
        ):
            raise ProviderConflict("Data resource ownership differs")
        identities[key] = obj.metadata.uid
        if obj.metadata.deletion_timestamp:
            raise ProviderConflict("Data resource is deleting")
        ready = ready and _resource_is_ready(kind, obj)
    return ready, identities
