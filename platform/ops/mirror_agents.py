"""Copy reviewed multi-language agents into Cloud Run's existing Artifact Registry."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from controlplane.domain.instrumentation import IMAGES


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError("Agent mirror failed; check Artifact Registry access")
    return result.stdout.strip()


def main():
    manifest = json.loads((Path(__file__).parents[1] / "deployment/provider-environments.json").read_text())
    targets = manifest["gcp"]["tracing"]["agent_images"]
    registry = next(iter(targets.values())).split("/")[0]
    with tempfile.TemporaryDirectory(prefix="oneport-registry-") as directory:
        env = {**os.environ, "DOCKER_CONFIG": directory}
        run(["docker", "login", registry, "--username", "_json_key", "--password-stdin"], input=os.environ["ONEPORT_GCP_CREDENTIALS_JSON"], env=env)
        for kind, target in targets.items():
            source = IMAGES[kind]
            expected = target.split("@", 1)[1]
            if expected != source.split("@", 1)[1]:
                raise ValueError("Unreviewed agent digest")
            tag = target.split("@", 1)[0] + ":" + expected.replace(":", "-")
            try:
                current = run(["docker", "buildx", "imagetools", "inspect", tag, "--format", "{{.Manifest.Digest}}"], env=env)
            except RuntimeError:
                current = None
            if current != expected:
                run(["docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", tag, source], env=env)
                current = run(["docker", "buildx", "imagetools", "inspect", tag, "--format", "{{.Manifest.Digest}}"], env=env)
            if current != expected:
                raise RuntimeError("Mirrored agent digest mismatch")
            print(f"Verified {kind} agent mirror")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Agent mirror failed; credentials and registry responses omitted") from None
