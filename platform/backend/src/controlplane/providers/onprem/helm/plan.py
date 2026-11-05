"""Compile the common application request into a constrained chart input."""

import hashlib
import re

from controlplane.domain.models import DeploymentRequest, ImageSource, Strategy
from controlplane.providers.contracts import ReleaseKey
from controlplane.providers.onprem.digest import sha256_json
from controlplane.providers.onprem.helm.models import ChartArtifact, HelmEnvironment, HelmPlan

IDENTITY_PATTERN = re.compile(r"[a-z](?:[a-z0-9-]*[a-z0-9])?")
SECRET_UID_PATTERN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
MAX_IMAGE_PULL_SECRETS = 5
MIN_PRIVILEGED_PORT = 1024
DEFAULT_CANARY_PERCENT = 10
DEFAULT_CANARY_HOLD_SECONDS = 30
REVIEWED_FAILURE_THRESHOLD = 3


def fingerprint(value: dict) -> str:
    return sha256_json(value, compact=True)


def release_name(application: str, environment: str, *, data: bool = False) -> str:
    for value, maximum in ((application, 30), (environment, 63)):
        if len(value) > maximum or not IDENTITY_PATTERN.fullmatch(value):
            raise ValueError("Invalid application/environment identity")
    key = hashlib.sha256((application + "\0" + environment).encode()).hexdigest()[:10]
    return f"op-{application[:16]}-{key}-{'data' if data else 'app'}"


def _blue_green_values(request: DeploymentRequest, env: HelmEnvironment) -> dict:
    policy = request.rollout
    if policy and (policy.promotion != "automatic" or not policy.auto_rollback):
        raise ValueError("Helm requires automatic promotion and rollback")
    delay = policy.bake_seconds if policy else env.scale_down_delay_seconds
    if not 30 <= delay <= 300:
        raise ValueError("Previous version retention must be 30..300 seconds")
    return {
        "type": "blueGreen",
        "promotion": "automatic",
        "scaleDownDelaySeconds": delay,
        "progressDeadlineSeconds": env.progress_deadline_seconds,
    }


def _canary_values(request: DeploymentRequest, env: HelmEnvironment) -> dict:
    policy = request.rollout
    if policy and (policy.promotion != "automatic" or not policy.auto_rollback):
        raise ValueError("Canary requires automatic promotion and rollback")
    percent = policy.canary_percent if policy else DEFAULT_CANARY_PERCENT
    hold = policy.hold_seconds if policy else DEFAULT_CANARY_HOLD_SECONDS
    delay = policy.bake_seconds if policy else env.scale_down_delay_seconds
    if not 5 <= hold <= 300 or not 30 <= delay <= 300:
        raise ValueError("Canary hold must be 5..300 seconds and retention 30..300 seconds")
    if policy and policy.failure_threshold != REVIEWED_FAILURE_THRESHOLD:
        raise ValueError("Canary health analysis uses the reviewed chart policy")
    return {
        "type": "canary",
        "promotion": "automatic",
        "scaleDownDelaySeconds": delay,
        "progressDeadlineSeconds": max(env.progress_deadline_seconds, hold * 3 + 300),
        "canary": {
            "weights": [percent, 50, 100] if percent < 50 else [percent, 100],
            "holdSeconds": hold,
        },
    }


def strategy_values(request: DeploymentRequest, env: HelmEnvironment) -> dict:
    if request.strategy == Strategy.BLUE_GREEN:
        return _blue_green_values(request, env)
    if request.strategy != Strategy.CANARY:
        raise ValueError("Helm supports blue-green or canary")
    return _canary_values(request, env)


def _validate_scope(request: DeploymentRequest, env: HelmEnvironment) -> None:
    if (request.application, request.environment_id) != (env.application, env.environment_id):
        raise ValueError("Request does not match the registered environment")
    if not isinstance(request.source, ImageSource) or request.architecture != "X86_64":
        raise ValueError("Helm requires a registry digest for linux/amd64")
    if request.port < MIN_PRIVILEGED_PORT or env.exceeds_ceiling(request):
        raise ValueError("Request exceeds the registered chart resource limits")


def _secret_refs(request: DeploymentRequest, env: HelmEnvironment) -> list[dict]:
    refs = []
    for binding in request.secret_bindings:
        ref = env.secret_refs.get(binding.binding)
        if ref is None or not ref.uid or not SECRET_UID_PATTERN.fullmatch(ref.uid):
            raise ValueError("An immutable registered Secret UID is required")
        refs.append({"env": binding.name, "name": ref.name, "key": ref.key, "uid": ref.uid})
    names = {r["env"] for r in refs}
    if request.resources.database == "postgresql" and "DATABASE_URL" not in names:
        raise ValueError("PostgreSQL requires a registered DATABASE_URL Secret binding")
    if request.resources.redis and "REDIS_URL" not in names:
        raise ValueError("Redis requires a registered REDIS_URL Secret binding")
    return refs


def _chart_values(
    request: DeploymentRequest,
    env: HelmEnvironment,
    release: ReleaseKey,
    refs: list[dict],
    strategy: dict,
    published: bool,
) -> dict:
    return {
        "application": request.application,
        "environment": request.environment_id,
        "releaseId": release.value,
        "image": request.source.image,
        "imagePullSecrets": env.image_pull_secrets,
        "replicas": request.replicas,
        "port": request.port,
        "healthPath": request.health_path,
        "env": request.env,
        "secretBindings": refs,
        "resources": {
            "cpuRequestMilli": 100,
            "cpuLimitMilli": int(float(request.cpu) * 1000),
            "memoryRequestMi": 128,
            "memoryLimitMi": request.memory_mib,
        },
        "security": {
            "runAsUser": env.run_as_user,
            "runAsGroup": env.run_as_group,
            "readOnlyRootFilesystem": env.read_only_root_filesystem,
        },
        "route": {"enabled": published, "hostname": env.hostname},
        "strategy": strategy,
    }


def app_plan(
    request: DeploymentRequest,
    env: HelmEnvironment,
    release: ReleaseKey,
    artifact: ChartArtifact,
    *,
    published: bool = False,
) -> HelmPlan:
    _validate_scope(request, env)
    strategy = strategy_values(request, env)
    if request.migration or request.resources.database_mode == "dedicated":
        raise ValueError("Helm migration/dedicated data requires a separate durable workflow")
    refs = _secret_refs(request, env)
    if len(env.image_pull_secrets) > MAX_IMAGE_PULL_SECRETS:
        raise ValueError("Chart supports at most five image pull Secrets")
    identity = {
        "release": release_name(request.application, request.environment_id),
        "namespace": env.namespace,
        "chart_sha256": artifact.sha256,
        "values": _chart_values(request, env, release, refs, strategy, published),
    }
    return HelmPlan(**identity, fingerprint=fingerprint(identity))


def validate_plan(plan: HelmPlan) -> None:
    identity = plan.model_dump(exclude={"fingerprint"})
    if fingerprint(identity) != plan.fingerprint:
        raise ValueError("Helm plan changed after preparation")
    expected = release_name(
        plan.values["application"],
        plan.values["environment"],
        data=plan.release.endswith("-data"),
    )
    if expected != plan.release:
        raise ValueError("Helm release identity mismatch")
