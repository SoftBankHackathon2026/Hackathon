"""Worker-facing candidate gateway; never constructs credentials or SDK clients.

Prepare and submit are deliberately separate checkpoints. Persist the request,
intent and returned checkpoint between calls. This is NOT a durable worker and
must not be wired to an HTTP request handler until the journal/lease exists.
"""

import hashlib
import json
from typing import Literal, Protocol

from controlplane.domain.models import DeploymentRequest, ImageSource, Model, Runtime, Strategy
from controlplane.providers.contracts import (
    Observation,
    ProviderConflict,
    ReleaseKey,
    Submission,
)


class EcsCheckpoint(Model):
    runtime: Literal[Runtime.AWS_ECS] = Runtime.AWS_ECS
    task_definition_arn: str


Checkpoint = EcsCheckpoint


class PreparedCandidate(Model):
    fingerprint: str
    release: ReleaseKey
    checkpoint: Checkpoint


class CandidateHandle(Model):
    prepared: PreparedCandidate
    submission: Submission
    fingerprint: str


class CandidateDriver(Protocol):
    runtime: Runtime

    def configuration(self) -> dict: ...

    def prepare(self, request: DeploymentRequest, release: ReleaseKey) -> Checkpoint: ...

    def submit(
        self, request: DeploymentRequest, release: ReleaseKey, checkpoint: Checkpoint
    ) -> Submission: ...

    def observe(self, request: DeploymentRequest, handle: CandidateHandle) -> Observation: ...


class CandidateGateway:
    """Routes an app+environment to an explicitly registered infrastructure binding.

    A candidate is common to blue/green and canary. Rolling-in-place is a separate
    operation and is rejected here. READY is provider readiness, never a release
    success or authorization to route production traffic.
    """

    def __init__(self, drivers: dict[tuple[str, str], CandidateDriver]):
        self._drivers = dict(drivers)

    def registered_driver(self, application: str, environment_id: str) -> CandidateDriver:
        driver = self._drivers.get((application, environment_id))
        if driver is None:
            raise ValueError("이 앱·환경에 등록된 배포 모듈이 없습니다.")
        return driver

    def _driver(self, request: DeploymentRequest) -> CandidateDriver:
        if not isinstance(request.source, ImageSource):
            raise TypeError("빌드를 완료하고 digest가 고정된 이미지로 요청하세요.")
        if request.strategy not in {Strategy.BLUE_GREEN, Strategy.CANARY}:
            raise ValueError("후보 배포 경로는 Blue/Green·Canary만 지원합니다.")
        return self.registered_driver(request.application, request.environment_id)

    @staticmethod
    def _fingerprint(
        request: DeploymentRequest,
        release: ReleaseKey,
        driver: CandidateDriver,
        checkpoint: Checkpoint,
    ) -> str:
        payload = {
            "request": request.model_dump(mode="json"),
            "release": release.value,
            "runtime": driver.runtime,
            "binding": driver.configuration(),
            "checkpoint": checkpoint.model_dump(mode="json"),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def prepare(self, request: DeploymentRequest, release: ReleaseKey) -> PreparedCandidate:
        driver = self._driver(request)
        checkpoint = driver.prepare(request, release)
        if checkpoint.runtime != driver.runtime:
            raise ProviderConflict("배포 모듈이 다른 Provider의 체크포인트를 반환했습니다.")
        return PreparedCandidate(
            fingerprint=self._fingerprint(request, release, driver, checkpoint),
            release=release,
            checkpoint=checkpoint,
        )

    def _check(self, request: DeploymentRequest, prepared: PreparedCandidate) -> CandidateDriver:
        driver = self._driver(request)
        if (
            prepared.checkpoint.runtime != driver.runtime
            or self._fingerprint(request, prepared.release, driver, prepared.checkpoint)
            != prepared.fingerprint
        ):
            raise ProviderConflict("준비 이후 요청 또는 인프라 연결이 변경됐습니다.")
        return driver

    def submit(self, request: DeploymentRequest, prepared: PreparedCandidate) -> CandidateHandle:
        driver = self._check(request, prepared)
        submission = driver.submit(request, prepared.release, prepared.checkpoint)
        if submission.provider != driver.runtime:
            raise ProviderConflict("배포 모듈의 응답 Provider가 일치하지 않습니다.")
        return CandidateHandle(
            prepared=prepared,
            submission=submission,
            fingerprint=self._handle_fingerprint(prepared, submission),
        )

    @staticmethod
    def _handle_fingerprint(prepared: PreparedCandidate, submission: Submission) -> str:
        # Corruption/mix-up detection, not authentication. Only a trusted journal
        # may supply these records; never accept them directly from an API client.
        return hashlib.sha256(
            json.dumps(
                {
                    "prepared": prepared.model_dump(mode="json"),
                    "submission": submission.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def observe(self, request: DeploymentRequest, handle: CandidateHandle) -> Observation:
        driver = self._check(request, handle.prepared)
        if (
            handle.submission.provider != driver.runtime
            or self._handle_fingerprint(handle.prepared, handle.submission) != handle.fingerprint
        ):
            raise ProviderConflict("다른 Provider의 배포 결과입니다.")
        return driver.observe(request, handle)
