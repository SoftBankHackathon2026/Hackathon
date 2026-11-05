"""Durable bridge to the team's process-local GCP API. Never replay an uncertain POST."""

import re
from enum import StrEnum

import httpx

from controlplane.domain.models import Runtime
from controlplane.execution.gcp_bridge.metadata import (
    PHASES,
    TERMINAL,
    RemoteStatus,
    public_metadata,
)
from controlplane.execution.reconciler import ReleaseReconciler
from controlplane.providers.contracts import ProviderUnavailable
from controlplane.providers.gcp_bridge.models import GcpRequest

INTAKE_REJECTIONS = {400, 401, 403, 404, 409, 422, 429}
RECEIPT_ID = re.compile(r"[0-9a-f]{32}")


class ReleaseState(StrEnum):
    QUEUED = "queued"
    REMOTE_SUBMITTING = "remote_submitting"
    REMOTE_WAITING = "remote_waiting"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class GcpRuntime:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.http = httpx.Client(
            base_url=settings.upstream_url,
            timeout=httpx.Timeout(15, connect=3),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self):
        self.http.close()


class GcpWorker(ReleaseReconciler):
    queue_runtime = Runtime.GCP_CLOUD_RUN

    def uncertain(self, conn, row, cp, code):
        self._move(
            conn,
            row,
            ReleaseState.UNCERTAIN,
            "GCP 실행 결과를 대조해야 합니다. 요청을 자동 재전송하지 않습니다.",
            cp,
            error_code=code,
        )

    def _step(self, conn, row):
        settings = self.runtime.settings
        cp = dict(row["checkpoint"])
        request = GcpRequest.model_validate(row["request"])
        settings.validate_request(request)
        if row["binding_fingerprint"] != settings.fingerprint():
            return self.uncertain(conn, row, cp, "gcp_binding_changed")
        if row["expired"]:
            return self._expire(conn, row, cp)
        if not cp.get("remote_id") and (
            row["state"] == ReleaseState.REMOTE_SUBMITTING or cp.get("submission_intent")
        ):
            return self.uncertain(conn, row, cp, "gcp_submission_unconfirmed")
        if row["state"] == ReleaseState.QUEUED and not cp.get("remote_id"):
            return self._submit(conn, row, cp, request)
        if not cp.get("remote_id"):
            return self.uncertain(conn, row, cp, "gcp_unknown_phase")
        return self._observe(conn, row, cp, request)

    def _expire(self, conn, row, cp):
        if (
            row["state"] == ReleaseState.QUEUED
            and not cp.get("submission_intent")
            and not cp.get("remote_id")
        ):
            return self._move(
                conn,
                row,
                ReleaseState.FAILED,
                "접수 기한이 만료됐습니다.",
                cp,
                error_code="deadline_exceeded",
            )
        return self.uncertain(conn, row, cp, "gcp_deadline_exceeded")

    def _submit(self, conn, row, cp, request):
        cp["submission_intent"] = True
        self._move(
            conn, row, ReleaseState.REMOTE_SUBMITTING, "GCP 배포 요청 의도를 저장했습니다.", cp
        )
        try:
            response = self.runtime.http.post(
                "/api/v1/deployments",
                json={
                    "repository_url": request.source.repository,
                    "app_id": request.application,
                    "provider": "gcp",
                    "strategy": request.strategy,
                },
            )
            if response.status_code in INTAKE_REJECTIONS:
                return self._move(
                    conn,
                    row,
                    ReleaseState.FAILED,
                    "GCP API가 접수를 거절했습니다.",
                    cp,
                    error_code=f"gcp_intake_{response.status_code}",
                )
            if response.status_code != 202:
                return self.uncertain(conn, row, cp, "gcp_submission_unconfirmed")
            remote_id = response.json().get("deployment_id")
            if not isinstance(remote_id, str) or not RECEIPT_ID.fullmatch(remote_id):
                return self.uncertain(conn, row, cp, "gcp_invalid_receipt")
            cp["remote_id"] = remote_id
            self._move(
                conn,
                row,
                ReleaseState.REMOTE_WAITING,
                "GCP API 접수 완료. 빌드·배포 진행을 조회합니다.",
                cp,
            )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            self.uncertain(conn, row, cp, "gcp_submission_unconfirmed")

    def _fetch_status(self, cp):
        try:
            response = self.runtime.http.get(f"/api/v1/deployments/{cp['remote_id']}")
        except httpx.HTTPError:
            raise ProviderUnavailable("GCP status temporarily unavailable") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailable("GCP status temporarily unavailable")
        return response

    def _observe(self, conn, row, cp, request):
        settings = self.runtime.settings
        response = self._fetch_status(cp)
        if response.status_code == 404:
            return self.uncertain(conn, row, cp, "gcp_history_lost")
        if response.status_code != 200:
            return self.uncertain(conn, row, cp, "gcp_status_rejected")
        data = response.json()
        if (
            not isinstance(data, dict)
            or data.get("deployment_id") != cp["remote_id"]
            or data.get("provider") != "gcp"
            or data.get("app_id") != request.application
            or data.get("repository_url") != request.source.repository
        ):
            return self.uncertain(conn, row, cp, "gcp_receipt_mismatch")
        status = data.get("status")
        if not isinstance(status, str) or status not in PHASES:
            return self.uncertain(conn, row, cp, "gcp_unknown_status")
        metadata = public_metadata(data, settings)
        old_history = cp.get("public", {}).get("history", [])
        history = metadata.get("history", old_history)
        if history[: len(old_history)] != old_history or any(
            entry["status"] in TERMINAL and entry["status"] != status for entry in history
        ):
            return self.uncertain(conn, row, cp, "gcp_history_changed")
        metadata["history"] = history
        if status == RemoteStatus.SUCCESS and not self._success_verified(metadata):
            return self.uncertain(conn, row, cp, "gcp_success_unverified")
        cp.update(remote_status=status, public=metadata)
        if metadata.get("image_uri"):
            cp["image"] = metadata["image_uri"]
        for entry in history[len(old_history) :]:
            self._upstream_move(conn, row, cp, entry["status"])
        self._upstream_move(conn, row, cp, status)
        if status not in TERMINAL:
            self._poll(conn, row, cp)

    @staticmethod
    def _success_verified(metadata):
        return bool(
            metadata.get("url")
            and metadata.get("revision")
            and metadata.get("image_uri")
            and metadata.get("health") == "OK"
        )

    def _upstream_move(self, conn, row, cp, status):
        phase, message = PHASES[status]
        if row["state"] == phase:
            return self.store.defer(conn, row, seconds=0, checkpoint=cp)
        if status == RemoteStatus.FAILED and cp["public"].get("rollback_status") == "SUCCESS":
            message = "신규 배포 실패 · 이전 버전 복구 완료"
        self._move(
            conn,
            row,
            phase,
            message,
            cp,
            error_code="gcp_deployment_failed"
            if status == RemoteStatus.FAILED
            else ("gcp_provider_uncertain" if status == RemoteStatus.UNCERTAIN else None),
            url=cp["public"].get("url"),
        )
