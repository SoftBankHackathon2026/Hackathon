"""The CodeBuild script passes the verified commit, and nothing else, into the image build."""

import importlib.util
import json
from pathlib import Path

path = Path(__file__).parents[2] / "ops" / "build" / "build_image.py"
spec = importlib.util.spec_from_file_location("build_image", path)
build_image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_image)

COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


def test_build_receives_only_the_verified_commit(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "Dockerfile").write_text("FROM scratch\n")
    for name, value in {
        "CODEBUILD_SRC_DIR": str(tmp_path),
        "ONEPORT_CONTEXT": "app",
        "ONEPORT_DOCKERFILE": "app/Dockerfile",
        "ONEPORT_COMMIT": COMMIT,
        "ONEPORT_IMAGE": "docker.io/oneport/application:build-" + "c" * 24,
        "DOCKERHUB_USERNAME": "user",
        "DOCKERHUB_PASSWORD": "token",
        "AWS_SESSION_TOKEN": "cloud-credential",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(build_image.subprocess, "check_output", lambda *a, **k: COMMIT + "\n")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "--metadata-file" in argv:
            Path(argv[argv.index("--metadata-file") + 1]).write_text(
                json.dumps({"containerimage.digest": DIGEST})
            )

    monkeypatch.setattr(build_image, "run", run)
    written = {}
    write_text = Path.write_text

    def record(self, text):
        # The digest goes to a fixed path outside the test's directory.
        if str(self) == "/tmp/oneport-image-digest":
            written[str(self)] = text
        else:
            write_text(self, text)

    monkeypatch.setattr(Path, "write_text", record)
    build_image.main()

    argv, kwargs = calls[-1]
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--build-arg"] == [
        f"GIT_SHA={COMMIT}"
    ]
    assert not any(key.startswith(("AWS_", "DOCKERHUB_")) for key in kwargs["env"])
    assert written["/tmp/oneport-image-digest"] == DIGEST
