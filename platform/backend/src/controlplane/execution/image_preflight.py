from controlplane.providers.contracts import ProviderRejected, ProviderUnavailable
from controlplane.providers.public_images import verify_public_image

MAX_REGISTRY_RETRIES = 5
MAX_BACKOFF_SECONDS = 30
PUBLIC_REGISTRY_PREFIX = "docker.io/"


def _needs_verification(row, image: str) -> bool:
    if row["state"] != "queued" or row["expired"] or not image.startswith(PUBLIC_REGISTRY_PREFIX):
        return False
    return row["checkpoint"].get("image_verification", {}).get("image") != image


def _registry_unavailable(store, conn, row, checkpoint: dict) -> None:
    attempts = checkpoint.get("image_transport_retries", 0) + 1
    checkpoint["image_transport_retries"] = attempts
    if attempts <= MAX_REGISTRY_RETRIES:
        store.defer(conn, row, seconds=min(2**attempts, MAX_BACKOFF_SECONDS), checkpoint=checkpoint)
        return
    store.transition(
        conn,
        row,
        "failed",
        "이미지 저장소를 확인하지 못해 배포를 시작하지 않았습니다.",
        checkpoint=checkpoint,
        error_code="image_registry_unavailable",
    )


def prepare_public_image(store, conn, row):
    image = row["request"].get("source", {}).get("image", "")
    if not _needs_verification(row, image):
        return True
    checkpoint = dict(row["checkpoint"])
    try:
        evidence = verify_public_image(image, row["request"].get("architecture", "X86_64"))
    except ProviderUnavailable:
        _registry_unavailable(store, conn, row, checkpoint)
        return False
    except ProviderRejected as exc:
        store.transition(
            conn,
            row,
            "failed",
            "공개 이미지 접근·digest·CPU 아키텍처 검증 실패. 배포하지 않았습니다.",
            error_code=str(exc),
        )
        return False
    checkpoint["image_verification"] = evidence
    store.transition(
        conn,
        row,
        "queued",
        "공개 이미지 digest·CPU 아키텍처 확인 완료.",
        checkpoint=checkpoint,
    )
    return False
