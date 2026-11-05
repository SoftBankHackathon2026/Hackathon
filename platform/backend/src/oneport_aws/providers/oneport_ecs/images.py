"""Read the image manifest (metadata only) before registering a task definition:
does the digest exist in ECR, and which CPU architectures does it contain?"""

import json
import urllib.request

from botocore.exceptions import BotoCoreError, ClientError

from controlplane.providers.contracts import ProviderRejected, ProviderUnavailable
from controlplane.providers.public_images import verify_public_image
from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment

_INDEX = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
_ACCEPTED = sorted(
    _INDEX
    | {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_ARCH = {"amd64": "X86_64", "arm64": "ARM64"}


def architectures(
    ecr, env: OneportEcsEnvironment, image: str, architecture: str = "X86_64"
) -> set[str] | None:
    """Fargate architectures in the image; None if the config blob is unreadable."""
    if image.startswith("docker.io/"):
        verify_public_image(image, architecture)
        return {architecture}
    digest = image.rsplit("@", 1)[1]
    try:
        found = ecr.batch_get_image(
            repositoryName=env.repository,
            imageIds=[{"imageDigest": digest}],
            acceptedMediaTypes=_ACCEPTED,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "RepositoryNotFoundException":
            raise ProviderRejected("ECR 저장소에 이미지가 없습니다.") from None
        raise ProviderUnavailable("ECR 조회가 실패했습니다.") from None
    except BotoCoreError:
        raise ProviderUnavailable("ECR 조회가 실패했습니다.") from None
    if not found.get("images"):
        raise ProviderRejected("ECR에 해당 digest의 이미지가 없습니다.")
    item = found["images"][0]
    manifest = json.loads(item["imageManifest"])
    if (item.get("imageManifestMediaType") or manifest.get("mediaType")) in _INDEX:
        return {
            _ARCH[m["platform"]["architecture"]]
            for m in manifest.get("manifests", [])
            if m.get("platform", {}).get("os") == "linux"
            and m["platform"].get("architecture") in _ARCH
        }
    try:
        url = ecr.get_download_url_for_layer(
            repositoryName=env.repository, layerDigest=manifest["config"]["digest"]
        )["downloadUrl"]
        with urllib.request.urlopen(url, timeout=10) as blob:  # presigned ECR/S3 URL
            arch = json.load(blob).get("architecture")
    except Exception:  # noqa: BLE001 -- unreadable config: let ECS decide at start
        return None
    return {_ARCH[arch]} if arch in _ARCH else set()
