"""Durable build state machine. Build and deployment use separate journals and locks."""

import time

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.rows import dict_row

from controlplane.builds.models import BuildSettings, BuildState
from controlplane.builds.provider import BuildRejected, CodeBuild
from controlplane.builds.store import BuildStore
from controlplane.core import db
from controlplane.core.errors import RequestConflict
from controlplane.core.locks import AdvisoryLock, session_unlock, try_session_lock
from controlplane.providers.contracts import ProviderRejected, ProviderUnavailable
from controlplane.providers.interface import DeploymentSpec
from controlplane.providers.public_images import verify_public_image
from controlplane.schemas.accounts import Identity

SUBMIT_WINDOW_SECONDS = 240
MAX_TRANSPORT_RETRIES = 6
TRANSPORT_ERRORS = (BotoCoreError, ClientError, httpx.HTTPError, ProviderUnavailable)
REJECTIONS = (RequestConflict, ValueError, ProviderRejected)


class BuildWorker:
    def __init__(self, store, provider, deployments, clock=time.time):
        self.store, self.provider, self.deployments, self.clock = (
            store,
            provider,
            deployments,
            clock,
        )

    def tick(self):
        with self.store.connect() as conn:
            conn.row_factory = dict_row
            acquired = try_session_lock(conn, AdvisoryLock.SOURCE_BUILDS)
            conn.commit()
            if not acquired:
                return
            try:
                rows = conn.execute(
                    "SELECT * FROM source_builds WHERE state NOT IN ('succeeded','failed','uncertain') ORDER BY created_at"
                ).fetchall()
                conn.commit()
                for row in rows:
                    self.step(conn, row)
            finally:
                session_unlock(conn, AdvisoryLock.SOURCE_BUILDS)
                conn.commit()

    def step(self, conn, row):
        cp, state = dict(row["checkpoint"]), row["state"]
        handler = {
            BuildState.QUEUED: self._resolve,
            BuildState.RESOLVED: self._submit,
            BuildState.SUBMITTING: self._start,
            BuildState.BUILDING: self._observe,
            BuildState.DEPLOYING: self._deploy,
        }.get(state)
        if handler is None:
            return
        try:
            handler(conn, row, cp)
        except BuildRejected as exc:
            self.store.update(
                conn,
                row,
                BuildState.FAILED,
                "저장소·버전·빌드 결과를 확인하세요.",
                error_code=str(exc),
            )
        except TRANSPORT_ERRORS:
            self._retry_transport(conn, row, cp)
        except REJECTIONS:
            self.store.update(
                conn,
                row,
                BuildState.FAILED,
                "빌드 결과 또는 배포 설정을 확인하세요.",
                error_code="build_or_deployment_rejected",
            )
        except Exception:
            conn.rollback()
            self.store.update(
                conn,
                row,
                BuildState.UNCERTAIN,
                "빌드 결과를 확인해야 합니다.",
                error_code="build_internal_error",
            )

    def _retry_transport(self, conn, row, cp):
        cp["retries"] = cp.get("retries", 0) + 1
        if cp["retries"] >= MAX_TRANSPORT_RETRIES:
            self.store.update(
                conn,
                row,
                BuildState.UNCERTAIN,
                "빌드 서비스 응답을 확인해야 합니다.",
                checkpoint=cp,
                error_code="build_provider_unavailable",
            )
        else:
            self.store.update(
                conn, row, row["state"], "빌드 서비스 상태를 다시 확인합니다.", checkpoint=cp
            )

    def _resolve(self, conn, row, cp):
        cp["commit"] = self.provider.resolve(
            row["request"]["repository"], row["request"]["revision"]
        )
        self.store.update(
            conn, row, BuildState.RESOLVED, "소스 commit을 고정했습니다.", checkpoint=cp
        )

    def _submit(self, conn, row, cp):
        cp["submit_started"] = self.clock()
        self.store.update(
            conn, row, BuildState.SUBMITTING, "격리된 빌드를 요청합니다.", checkpoint=cp
        )

    def _start(self, conn, row, cp):
        if self.clock() - cp["submit_started"] > SUBMIT_WINDOW_SECONDS:
            self.store.update(
                conn,
                row,
                BuildState.UNCERTAIN,
                "빌드 접수 결과를 확인해야 합니다. 중복 빌드를 만들지 않았습니다.",
                error_code="build_submission_uncertain",
            )
            return
        cp["build_id"] = self.provider.start(row)
        self.store.update(
            conn,
            row,
            BuildState.BUILDING,
            "이미지를 빌드하고 Registry에 올립니다.",
            checkpoint=cp,
        )

    def _observe(self, conn, row, cp):
        result = self.provider.observe(cp["build_id"])
        status = result["buildStatus"]
        if status == "IN_PROGRESS":
            return
        if status != "SUCCEEDED":
            self.store.update(
                conn,
                row,
                BuildState.FAILED,
                "이미지 빌드에 실패했습니다. 빌드 로그를 확인하세요.",
                checkpoint=cp,
                error_code="codebuild_" + status.lower(),
            )
            return
        image = self.provider.result_image(row, result)
        verify_public_image(image, "X86_64")
        if row["request"].get("deploy_after_build", True):
            state = BuildState.DEPLOYING
            message = "공개 이미지 digest 검증 완료. 선택한 환경에 배포합니다."
        else:
            state = BuildState.SUCCEEDED
            message = "빌드·Registry 업로드·공개 이미지 검증 완료."
        self.store.update(conn, row, state, message, checkpoint=cp, image=image)

    def _deploy(self, conn, row, cp):
        payload = dict(row["request"]["deployment"])
        payload["source"] = {"kind": "image", "image": row["image"]}
        requester = (
            Identity(email=row["created_by"], team=row["team"], role="member")
            if row.get("team")
            else None
        )
        release = self.deployments.submit(
            DeploymentSpec.model_validate(payload), "source-build-" + row["id"], requester
        )
        self.store.update(
            conn,
            row,
            BuildState.SUCCEEDED,
            "빌드 완료. 앱 배포를 접수했습니다.",
            release_id=release.id,
        )


def main():
    import boto3
    from botocore.config import Config

    from controlplane.providers.loader import configured
    from controlplane.services.deployments import DeploymentService

    settings = BuildSettings.from_environment()
    if settings is None:
        raise SystemExit("Build configuration is required")
    registry, releases, _ = configured(require_token=False)
    releases.migrate()
    registry.initialize()
    store = BuildStore(releases.dsn)
    store.migrate()
    provider = CodeBuild(
        settings,
        boto3.client(
            "codebuild",
            region_name=settings.region,
            config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2}),
        ),
    )
    worker = BuildWorker(store, provider, DeploymentService(registry, releases))
    with db.pooling():
        while True:
            worker.tick()
            time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
