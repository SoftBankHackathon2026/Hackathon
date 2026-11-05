import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_argo import TERMINAL, advance, enqueue, tick
from test_argo import harness as harness  # noqa: F401
from test_argo import sdk as sdk  # noqa: F401

from controlplane.execution.onprem.runtime import ArgoRuntime
from controlplane.providers.contracts import ProviderConflict, ReleaseKey
from controlplane.providers.onprem import probe_job


def test_probe_lifecycle_pending_success_and_each_sample_has_own_job(sdk):
    key = ReleaseKey(value="a" * 24)
    kwargs = dict(image=sdk.request.source.image, probe_id="release-preview-0")
    assert probe_job.observe(sdk.provider, sdk.request, key, **kwargs) is None
    assert probe_job.observe(sdk.provider, sdk.request, key, **kwargs) is None
    assert sdk.api.creates == 1
    job = next(iter(sdk.api.objects.values()))
    pod = job["spec"]["template"]["spec"]
    assert not pod["automountServiceAccountToken"]
    assert "env" not in pod["containers"][0] and "volumes" not in pod
    assert job["spec"]["ttlSecondsAfterFinished"] == 300
    job["status"] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
    assert probe_job.observe(sdk.provider, sdk.request, key, **kwargs) is True
    assert (
        probe_job.observe(
            sdk.provider, sdk.request, key, **{**kwargs, "probe_id": "release-preview-1"}
        )
        is None
    )
    assert sdk.api.creates == 2


def test_failed_probe_and_foreign_job_are_not_success(sdk):
    key = ReleaseKey(value="a" * 24)
    kwargs = dict(image=sdk.request.source.image, probe_id="release-preview-0")
    probe_job.observe(sdk.provider, sdk.request, key, **kwargs)
    job = next(iter(sdk.api.objects.values()))
    job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    assert probe_job.observe(sdk.provider, sdk.request, key, **kwargs) is False
    job["metadata"]["labels"]["launchpad.io/application"] = "another-app"
    with pytest.raises(ProviderConflict):
        probe_job.observe(sdk.provider, sdk.request, key, **kwargs)


@pytest.mark.parametrize(
    "status,header", [(200, "correct"), (200, "wrong"), (503, "correct"), (302, "correct")]
)
def test_job_script_requires_http_200_exact_header_and_no_redirect(status, header):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("X-Launchpad-Release", header)
            if status == 302:
                self.send_header("Location", "/redirect")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                probe_job.SCRIPT,
                f"http://127.0.0.1:{server.server_port}/healthz",
                "correct",
            ],
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == (0 if status == 200 and header == "correct" else 1)
        assert result.stdout == b"" and result.stderr == b""
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pending_probe_does_not_consume_failure_budget(harness):
    h = harness
    row = enqueue(h, "job-probe-worker-test")
    advance(h, row, {"verifying"})
    h.runtime.probe.return_value = None
    for _ in range(15):
        tick(h)
    current = h.store.get(row["id"])
    assert current["state"] == "verifying"
    assert current["checkpoint"]["attempts"] == 0
    h.runtime.probe.return_value = True
    assert advance(h, row, TERMINAL)["state"] == "succeeded"


def test_job_mode_does_not_use_local_preview_http(sdk):
    from test_argo import ArgoSettings

    settings = ArgoSettings(
        environment=sdk.env,
        database_url="postgresql://unused",
        kubeconfig="/unused",
        context="test",
        server="https://cluster.example.com",
        allowed_images=[sdk.request.source.image],
        preview_probe_mode="job",
        preview_probe_image=sdk.request.source.image,
    )
    rt = ArgoRuntime(settings)
    rt.provider = sdk.provider
    assert (
        rt.probe(sdk.request, ReleaseKey(value="a" * 24), production=False, probe_id="one") is None
    )
    assert rt.http is None


def test_default_fingerprint_preserves_existing_release_compatibility(harness):
    settings = harness.runtime.settings
    legacy = settings.model_dump(
        mode="json",
        exclude={
            "database_url",
            "kubeconfig",
            "allowed_images",
            "preview_probe_mode",
            "preview_probe_image",
            "observability",  # Read-only telemetry was absent from legacy bindings.
        },
    )
    old_hash = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
    assert settings.fingerprint() == old_hash
    settings.preview_probe_mode = "job"
    settings.preview_probe_image = harness.request.source.image
    assert settings.fingerprint() != old_hash
