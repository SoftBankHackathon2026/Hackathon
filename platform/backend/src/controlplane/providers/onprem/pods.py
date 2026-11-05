"""Pure, credential-free Kubernetes manifests from the common deployment contract."""

from controlplane.domain.models import DeploymentRequest, ImageSource, Runtime
from controlplane.providers.capabilities import validate_provider_contract
from controlplane.providers.contracts import ReleaseKey
from controlplane.providers.onprem.bindings import KubernetesBinding
from controlplane.providers.onprem.digest import sha256_json

OWNER = "launchpad.io/application"
ENVIRONMENT = "launchpad.io/environment"
RELEASE = "launchpad.io/release"
FINGERPRINT = "launchpad.io/spec-fingerprint"

READINESS_PERIOD_SECONDS = 5
PROBE_TIMEOUT_SECONDS = 2


def validate(request: DeploymentRequest, env: KubernetesBinding) -> None:
    validate_provider_contract(request, Runtime.ONPREM_ARGO)
    if (request.application, request.environment_id) != (env.application, env.environment_id):
        raise ValueError("Request does not match the registered app/environment")
    if not isinstance(request.source, ImageSource):
        raise TypeError("Build the image and pin its digest first")
    if env.exceeds_ceiling(request):
        raise ValueError("Requested resources exceed the registered environment ceiling")
    if any(b.binding not in env.secret_refs for b in request.secret_bindings):
        raise ValueError("Unregistered Secret binding")
    names = {b.name for b in request.secret_bindings}
    if request.resources.database != "none" and "DATABASE_URL" not in names:
        raise ValueError("Provision the app DB and register DATABASE_URL first")
    if request.resources.redis and "REDIS_URL" not in names:
        raise ValueError("Provision Redis and register REDIS_URL first")


def labels(env: KubernetesBinding, release: ReleaseKey | None = None) -> dict:
    result = {OWNER: env.application, ENVIRONMENT: env.environment_id}
    if release:
        result[RELEASE] = release.value
    return result


def label_selector(selected: dict) -> str:
    return ",".join(f"{key}={value}" for key, value in selected.items())


def name(env: KubernetesBinding, release: ReleaseKey | None = None) -> str:
    return f"lp-{env.application}" + (f"-{release.value}" if release else "")


def seal(body: dict) -> dict:
    body["metadata"]["annotations"] = {FINGERPRINT: sha256_json(body)}
    return body


def manifest(
    kind: str,
    api: str,
    env: KubernetesBinding,
    object_name: str,
    spec: dict,
    release: ReleaseKey | None = None,
) -> dict:
    return seal(
        {
            "apiVersion": api,
            "kind": kind,
            "metadata": {
                "name": object_name,
                "namespace": env.namespace,
                "labels": labels(env, release),
            },
            "spec": spec,
        }
    )


def workload_pod_fields(env: KubernetesBinding) -> dict:
    return {
        "serviceAccountName": env.workload_service_account,
        "automountServiceAccountToken": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "nodeSelector": {"kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64"},
        "imagePullSecrets": [{"name": secret} for secret in env.image_pull_secrets],
    }


def http_probe(request: DeploymentRequest, *, failure_threshold: int) -> dict:
    return {
        "httpGet": {"path": request.health_path, "port": "http"},
        "periodSeconds": READINESS_PERIOD_SECONDS,
        "timeoutSeconds": PROBE_TIMEOUT_SECONDS,
        "failureThreshold": failure_threshold,
    }


def container_env(
    request: DeploymentRequest, env: KubernetesBinding, release: ReleaseKey
) -> list[dict]:
    values = [
        {"name": "PORT", "value": str(request.port)},
        {"name": "LAUNCHPAD_RELEASE_ID", "value": release.value},
    ]
    for binding in request.secret_bindings:
        ref = env.secret_refs[binding.binding]
        values.append(
            {
                "name": binding.name,
                "valueFrom": {"secretKeyRef": ref.model_dump(include={"name", "key"})},
            }
        )
    values.extend({"name": key, "value": value} for key, value in sorted(request.env.items()))
    return values


def app_container(request: DeploymentRequest, env: KubernetesBinding, release: ReleaseKey) -> dict:
    resources = {"cpu": request.cpu, "memory": f"{request.memory_mib}Mi"}
    return {
        "name": "app",
        "image": request.source.image,
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": request.port}],
        "env": container_env(request, env, release),
        "resources": {"requests": resources.copy(), "limits": resources.copy()},
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
        },
        "readinessProbe": http_probe(request, failure_threshold=3),
        "startupProbe": http_probe(request, failure_threshold=60),
    }


def pod_spec(request: DeploymentRequest, env: KubernetesBinding, release: ReleaseKey) -> dict:
    validate(request, env)
    return {
        **workload_pod_fields(env),
        "terminationGracePeriodSeconds": 30,
        "containers": [app_container(request, env, release)],
    }


def migration_job(request: DeploymentRequest, env: KubernetesBinding, release: ReleaseKey) -> dict:
    if request.migration is None:
        raise ValueError("A migration command is required")
    pod = pod_spec(request, env, release)
    container = pod["containers"][0]
    for field in ("ports", "readinessProbe", "startupProbe"):
        container.pop(field)
    container.update(command=request.migration.argv, args=[])
    pod["restartPolicy"] = "Never"
    return manifest(
        "Job",
        "batch/v1",
        env,
        name(env, release) + "-mig",
        {
            "backoffLimit": 0,
            "activeDeadlineSeconds": request.migration.timeout_seconds,
            "completions": 1,
            "parallelism": 1,
            "template": {"metadata": {"labels": labels(env, release)}, "spec": pod},
        },
        release,
    )
