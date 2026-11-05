"""One internal HTTP observation per durable sample; no kubeconfig or app secrets in Pods."""

import hashlib

from controlplane.domain.models import ImageSource
from controlplane.providers.contracts import ReleaseKey
from controlplane.providers.onprem import manifests, pods
from controlplane.providers.onprem.primitives import JobOutcome, job_outcome

# No redirects, credentials, response body logging or arbitrary operator commands.
SCRIPT = """import sys, urllib.request
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(sys.argv[1], timeout=5) as response:
        ok = response.status == 200 and response.headers.get("X-Launchpad-Release") == sys.argv[2]
except Exception:
    ok = False
sys.exit(0 if ok else 1)
"""


def probe_container(image: str, url: str, release: ReleaseKey) -> dict:
    return {
        "name": "probe",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "command": ["python", "-c", SCRIPT],
        "args": [url, release.value],
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"cpu": "100m", "memory": "64Mi"},
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
    }


def manifest(request, env, release: ReleaseKey, *, image, probe_id):
    pods.validate(request, env)
    image = ImageSource(image=image).image
    if not probe_id or len(probe_id) > 128:
        raise ValueError("Bounded durable probe ID required")
    suffix = hashlib.sha256(probe_id.encode()).hexdigest()[:16]
    base_name = manifests.name(env)
    url = (
        f"http://{base_name}-preview.{env.namespace}.svc.{env.cluster_domain}{request.health_path}"
    )
    return pods.manifest(
        "Job",
        "batch/v1",
        env,
        f"{base_name[:35]}-probe-{suffix}",
        {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 60,
            "ttlSecondsAfterFinished": 300,
            "completions": 1,
            "parallelism": 1,
            "template": {
                "metadata": {
                    "labels": {
                        **pods.labels(env, release),
                        "launchpad.io/component": "preview-probe",
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    **pods.workload_pod_fields(env),
                    "containers": [probe_container(image, url, release)],
                },
            },
        },
        release,
    )


def observe(provider, request, release, *, image, probe_id):
    desired = manifest(request, provider.environment, release, image=image, probe_id=probe_id)
    outcome = job_outcome(provider.ensure(desired))
    if outcome == JobOutcome.FAILED:
        return False
    if outcome == JobOutcome.SUCCEEDED:
        return True
    return None
