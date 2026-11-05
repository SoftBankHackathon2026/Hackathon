"""Verified public observations only; never persist raw provider errors or log bodies."""

import re
from enum import StrEnum
from typing import NamedTuple


class RemoteStatus(StrEnum):
    PENDING = "PENDING"
    CLONING = "CLONING"
    BUILDING = "BUILDING"
    PUSHING = "PUSHING"
    DEPLOYING = "DEPLOYING"
    VERIFYING = "VERIFYING"
    PROMOTING = "PROMOTING"
    CHECKING = "CHECKING"
    RESTORING = "RESTORING"
    RESTORE_CHECKING = "RESTORE_CHECKING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class Phase(NamedTuple):
    state: str
    message: str


PHASES = {
    RemoteStatus.PENDING: Phase("queued", "배포 요청을 접수했습니다."),
    RemoteStatus.CLONING: Phase("cloning", "저장소를 가져오고 있습니다."),
    RemoteStatus.BUILDING: Phase("building", "Docker 이미지를 빌드하고 있습니다."),
    RemoteStatus.PUSHING: Phase("pushing", "Artifact Registry에 이미지를 업로드하고 있습니다."),
    RemoteStatus.DEPLOYING: Phase("deploying", "Cloud Run 후보 Revision을 배포하고 있습니다."),
    RemoteStatus.VERIFYING: Phase("verifying", "후보 Revision Health Check를 확인하고 있습니다."),
    RemoteStatus.PROMOTING: Phase("promoting", "후보를 Production으로 승격하고 있습니다."),
    RemoteStatus.CHECKING: Phase("checking", "Production Health Check를 확인하고 있습니다."),
    RemoteStatus.RESTORING: Phase(
        "restoring", "Health Check 실패로 이전 Revision을 복구하고 있습니다."
    ),
    RemoteStatus.RESTORE_CHECKING: Phase(
        "restore_checking", "이전 Revision 복구를 확인하고 있습니다."
    ),
    RemoteStatus.SUCCESS: Phase("succeeded", "배포가 완료되었습니다."),
    RemoteStatus.FAILED: Phase("failed", "신규 배포가 실패했습니다."),
    RemoteStatus.UNCERTAIN: Phase(
        "uncertain", "클라우드 실행 결과 확인 필요. 자동 재접수하지 않습니다."
    ),
}
TERMINAL = {RemoteStatus.SUCCESS, RemoteStatus.FAILED, RemoteStatus.UNCERTAIN}
ROLLBACK_STATUSES = {"SUCCESS", "FAILED", "UNCERTAIN", "NOT_REQUIRED"}


def public_metadata(data, settings):
    output = {}
    patterns = {
        "project_id": r"[a-z][a-z0-9-]{4,62}",
        "source_commit": r"[0-9a-f]{40}",
        "image_uri": r"[a-z0-9-]+-docker\.pkg\.dev/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9._/-]+@sha256:[0-9a-f]{64}",
        **{
            key: re.escape(settings.application) + r"-[a-z0-9-]{1,58}"
            for key in ("revision", "previous_revision", "candidate_revision", "active_revision")
        },
    }
    for key, pattern in patterns.items():
        value = data.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            output[key] = value
    if "image_uri" in output:
        output["digest"] = output["image_uri"].rsplit("@", 1)[1]
    if data.get("health") == "OK":
        output["health"] = "OK"
    if data.get("rollback_status") in ROLLBACK_STATUSES:
        output["rollback_status"] = data["rollback_status"]
    if isinstance(data.get("url"), str) and data["url"].rstrip("/") == settings.service_url:
        output["url"] = settings.service_url
    if isinstance(data.get("history"), list):
        output["history"] = [
            {"status": entry["status"], "phase": PHASES[entry["status"]][0]}
            for entry in data["history"][:1000]
            if isinstance(entry, dict)
            and isinstance(entry.get("status"), str)
            and entry["status"] in PHASES
        ]
    return output
