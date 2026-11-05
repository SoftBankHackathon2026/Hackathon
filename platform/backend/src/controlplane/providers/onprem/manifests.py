"""One Rollout per app/environment; preview never gets an external Ingress."""

import hashlib

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import ReleaseKey
from controlplane.providers.onprem import pods
from controlplane.providers.onprem.models import ArgoEnvironment

HASH = "rollouts-pod-template-hash"


def name(env: ArgoEnvironment) -> str:
    suffix = hashlib.sha256(env.environment_id.encode()).hexdigest()[:8]
    return f"op-{env.application}-{suffix}"


def service(request: DeploymentRequest, env: ArgoEnvironment, *, preview: bool = False) -> dict:
    pods.validate(request, env)
    return pods.manifest(
        "Service",
        "v1",
        env,
        name(env) + ("-preview" if preview else "-active"),
        {
            "type": "ClusterIP",
            "selector": {**pods.labels(env), HASH: "unassigned"},
            "ports": [{"name": "http", "port": 80, "targetPort": "http", "protocol": "TCP"}],
        },
    )


def ingress(env: ArgoEnvironment) -> dict:
    obj = pods.manifest(
        "Ingress",
        "networking.k8s.io/v1",
        env,
        name(env),
        {
            "ingressClassName": env.ingress_class,
            "rules": [
                {
                    "host": env.hostname,
                    "http": {
                        "paths": [
                            {
                                "path": "/",
                                "pathType": "Prefix",
                                "backend": {
                                    "service": {
                                        "name": name(env) + "-active",
                                        "port": {"name": "http"},
                                    }
                                },
                            }
                        ]
                    },
                }
            ],
        },
    )
    obj["metadata"]["annotations"]["traefik.ingress.kubernetes.io/router.entrypoints"] = "websecure"
    return obj


def rollout(request: DeploymentRequest, env: ArgoEnvironment, release: ReleaseKey) -> dict:
    pod = pods.pod_spec(request, env, release)
    return pods.manifest(
        "Rollout",
        "argoproj.io/v1alpha1",
        env,
        name(env),
        {
            "replicas": request.replicas,
            "revisionHistoryLimit": 5,
            "progressDeadlineSeconds": 360,
            "progressDeadlineAbort": True,
            "selector": {"matchLabels": pods.labels(env)},
            "template": {
                "metadata": {"labels": pods.labels(env, release)},
                "spec": pod,
            },
            "strategy": {
                "blueGreen": {
                    "activeService": name(env) + "-active",
                    "previewService": name(env) + "-preview",
                    "autoPromotionEnabled": False,
                    "scaleDownDelaySeconds": env.scale_down_delay_seconds,
                }
            },
        },
    )


def bundle(request: DeploymentRequest, env: ArgoEnvironment, release: ReleaseKey) -> list[dict]:
    return [
        service(request, env),
        service(request, env, preview=True),
        rollout(request, env, release),
        ingress(env),
    ]
