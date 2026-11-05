"""Pre-deploy stage (infra Notion §3 step 2): Git commit -> linux/amd64 image ->
ECR `oneport/<app-id>:<git-sha>` -> a DeploymentRequest with an ImageSource digest.

Repo rule: building is separate from deployment; the candidate gateway only
accepts pinned digests. ECR tags are IMMUTABLE, so a commit that was already
pushed is reused, never rebuilt. The repository is created by the ECR template
on first push. boto3 cannot push layers; the docker CLI does, with a token from
ecr.get_authorization_token (allowed for oneport-deployer).
"""

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from controlplane.domain.models import DeploymentRequest, GitSource, ImageSource
from controlplane.providers.contracts import (
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
)
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment

Log = Callable[[str], None]


class BuildFailed(ProviderRejected):
    """git/docker reported an error; the output tail is in `detail`, never secrets."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


def run_command(
    args: list[str], log: Log, *, timeout: int, stdin: str | None = None, cwd: Path | None = None
):
    """Stream output to `log`; raise BuildFailed on non-zero exit."""
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if stdin is not None:
        proc.stdin.write(stdin)
        proc.stdin.close()
    tail: list[str] = []
    deadline = time.monotonic() + timeout
    for line in proc.stdout:
        log(line.rstrip())
        tail = [*tail, line.rstrip()][-20:]
        if time.monotonic() > deadline:
            proc.kill()
            raise SubmissionUncertain(
                f"`{args[0]} {args[1]}` 시간 초과. 레지스트리를 조회한 뒤 판단하세요."
            )
    if proc.wait() != 0:
        raise BuildFailed(f"`{args[0]} {args[1]}` 실패 (exit {proc.returncode})", "\n".join(tail))


class EcrImageBuilder:
    def __init__(
        self,
        ecr,
        environment: OneportEcsEnvironment,
        *,
        platform: str = "linux/amd64",
        timeout: int = 1200,
        run: Callable = run_command,
    ):
        self.ecr = ecr
        self.environment = environment
        self.platform = platform
        self.timeout = timeout
        self.run = run

    def existing_digest(self, tag: str) -> str | None:
        try:
            found = self.ecr.describe_images(
                repositoryName=self.environment.repository, imageIds=[{"imageTag": tag}]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "RepositoryNotFoundException",
                "ImageNotFoundException",
            }:
                return None
            raise ProviderUnavailable("ECR 조회가 실패했습니다.") from None
        except BotoCoreError:
            raise ProviderUnavailable("ECR 조회가 실패했습니다.") from None
        details = found.get("imageDetails", [])
        return details[0]["imageDigest"] if details else None

    def _configure_auth(self, log: Log, config: Path) -> None:
        try:
            auth = self.ecr.get_authorization_token()["authorizationData"][0]
        except (ClientError, BotoCoreError):
            raise ProviderRejected("ECR 인증 토큰을 받지 못했습니다.") from None
        # Write only this job's ECR auth. Avoid docker login and OS credential
        # helpers entirely: neither ~/.docker nor a shared keychain is mutated.
        # --config also changes plugin discovery. Preserve plugin locations,
        # never credentials/helpers, from the operator's Docker configuration.
        original = Path(os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")))
        plugin_dirs = [str(original / "cli-plugins")]
        try:
            extras = json.loads((original / "config.json").read_text()).get(
                "cliPluginsExtraDirs", []
            )
            if isinstance(extras, list):
                plugin_dirs.extend(value for value in extras if isinstance(value, str))
        except (OSError, ValueError):
            pass
        config_file = config / "config.json"
        config_file.write_text(
            json.dumps(
                {
                    "auths": {self.environment.ecr_registry: {"auth": auth["authorizationToken"]}},
                    "credsStore": "",
                    "cliPluginsExtraDirs": plugin_dirs,
                }
            )
        )
        config_file.chmod(0o600)
        log("ECR 인증 준비 완료 (작업 전용 설정)")

    def build(self, request: DeploymentRequest, log: Log = print) -> DeploymentRequest:
        """Return the same request with `source` replaced by the pushed digest."""
        source = request.source
        if not isinstance(source, GitSource):
            raise TypeError("빌드는 Git 커밋 소스만 받습니다.")
        env = self.environment
        if (request.application, request.environment_id) != (env.application, env.environment_id):
            raise ValueError("등록된 앱·환경만 빌드할 수 있습니다.")
        tag = source.commit  # GitSource pins a full 40-char SHA
        digest = self.existing_digest(tag)
        if digest:
            log(f"{env.repository_uri}:{tag} 이미 push됨 (IMMUTABLE) -> {digest} 재사용")
        else:
            digest = self._build_and_push(source, tag, log)
        image = ImageSource(image=f"{env.repository_uri}@{digest}")
        return request.model_copy(update={"source": image})

    def _build_and_push(self, source: GitSource, tag: str, log: Log) -> str:
        ref = f"{self.environment.repository_uri}:{tag}"
        with tempfile.TemporaryDirectory(prefix="oneport-build-") as tmp:
            config = Path(tmp) / "docker"
            config.mkdir(mode=0o700)
            self._configure_auth(log, config)
            src = Path(tmp) / "src"
            src.mkdir()
            log(f"$ git fetch {source.repository} {source.commit}")
            self.run(["git", "init", "-q"], log, cwd=src, timeout=30)
            self.run(
                ["git", "fetch", "-q", "--depth", "1", source.repository, source.commit],
                log,
                cwd=src,
                timeout=self.timeout,
            )
            self.run(["git", "checkout", "-q", "--detach", "FETCH_HEAD"], log, cwd=src, timeout=60)
            if not (src / source.dockerfile).is_file():
                raise BuildFailed(f"Dockerfile을 찾을 수 없습니다: {source.dockerfile}")
            metadata = Path(tmp) / "metadata.json"
            log(f"$ docker buildx build --platform {self.platform} --push -t {ref}")
            self.run(
                [
                    "docker",
                    "--config",
                    str(config),
                    "buildx",
                    "build",
                    "--platform",
                    self.platform,
                    "--provenance=false",  # one plain manifest per tag
                    "-f",
                    str(src / source.dockerfile),
                    "-t",
                    ref,
                    "--metadata-file",
                    str(metadata),
                    "--push",
                    str(src / source.context),
                ],
                log,
                timeout=self.timeout,
            )
            digest = (
                json.loads(metadata.read_text()).get("containerimage.digest")
                if metadata.exists()
                else None
            )
        digest = digest or self.existing_digest(tag)
        if not digest:
            raise SubmissionUncertain("push 결과 digest를 확인하지 못했습니다. ECR을 조회하세요.")
        log(f"pushed {self.environment.repository_uri}@{digest}")
        return digest
