"""Runs inside the ephemeral CodeBuild environment, not on the platform API server."""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


def run(argv, **kwargs):
    subprocess.run(argv, check=True, timeout=780, **kwargs)


def main():
    root = Path(os.environ["CODEBUILD_SRC_DIR"]).resolve()
    context = (root / os.environ["ONEPORT_CONTEXT"]).resolve()
    dockerfile = (root / os.environ["ONEPORT_DOCKERFILE"]).resolve()
    if not context.is_relative_to(root) or not context.is_dir():
        raise ValueError("Build context must stay inside the checkout")
    if not dockerfile.is_relative_to(root) or not dockerfile.is_file():
        raise ValueError("Dockerfile must stay inside the checkout")
    commit = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != os.environ["ONEPORT_COMMIT"]:
        raise ValueError("Checked out commit differs from approved source")
    image = os.environ["ONEPORT_IMAGE"]
    if not re.fullmatch(
        r"docker.io/[a-z0-9_-]+/[a-z0-9_.-]+:build-[a-f0-9]{24}", image
    ):
        raise ValueError("Invalid build output reference")
    with tempfile.TemporaryDirectory(prefix="oneport-build-") as temp:
        env = {**os.environ, "DOCKER_CONFIG": temp}
        run(
            [
                "docker",
                "login",
                "--username",
                env["DOCKERHUB_USERNAME"],
                "--password-stdin",
            ],
            input=env["DOCKERHUB_PASSWORD"] + "\n",
            text=True,
            env=env,
            stdout=subprocess.DEVNULL,
        )
        # No cloud credentials, app secrets, arbitrary build args, SSH forwarding,
        # host mounts or network entitlements are passed into the Docker build.
        # The one build arg is the verified commit, so an app can report what it
        # was built from; a Dockerfile that does not declare it is unaffected.
        clean = {
            k: v for k, v in env.items() if not k.startswith(("AWS_", "DOCKERHUB_"))
        }
        metadata = Path(temp) / "metadata.json"
        run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--provenance=false",
                "--push",
                "--metadata-file",
                str(metadata),
                "--build-arg",
                f"GIT_SHA={commit}",
                "-t",
                image,
                "-f",
                str(dockerfile),
                str(context),
            ],
            env=clean,
        )
        digest = json.loads(metadata.read_text()).get("containerimage.digest", "")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Build did not produce a digest")
        Path("/tmp/oneport-image-digest").write_text(digest)


if __name__ == "__main__":
    main()
