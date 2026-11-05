"""Durable Argo reconciliation. Argo owns Service selectors, this worker owns Release intent.

No automatic migration replay and no DB rollback. Failed releases with a previous
verified request restore that template and re-check both private/public endpoints.
"""

from dataclasses import dataclass
from typing import Any

from controlplane.domain.models import Runtime
from controlplane.execution.onprem import delete
from controlplane.execution.onprem.states import RESTORE_PREFIX, ArgoState, DrivePhase
from controlplane.execution.reconciler import ReleaseReconciler
from controlplane.providers.contracts import Phase, ProviderConflict, ReleaseKey
from controlplane.providers.onprem import Revision
from controlplane.providers.onprem.models import ArgoDeploymentRequest, ArgoObservation


@dataclass
class ArgoTick:
    conn: Any
    row: dict
    request: ArgoDeploymentRequest
    key: ReleaseKey
    cp: dict

    @property
    def state(self) -> str:
        return self.row["state"]


@dataclass
class Drive:
    tick: ArgoTick
    request: ArgoDeploymentRequest
    key: ReleaseKey
    obs: ArgoObservation
    restoring: bool

    def state(self, phase: DrivePhase) -> ArgoState:
        return ArgoState(RESTORE_PREFIX + phase if self.restoring else phase)


class ArgoWorker(ReleaseReconciler):
    queue_runtime = Runtime.ONPREM_ARGO

    def _delete(self, deletion):
        delete.step(deletion, self.runtime)

    def _goto(self, tick: ArgoTick, state, message, **kwargs):
        self._move(tick.conn, tick.row, state, message, tick.cp, **kwargs)

    def _wait(self, tick: ArgoTick):
        self._poll(tick.conn, tick.row, tick.cp)

    def _handlers(self):
        return {
            ArgoState.QUEUED: self._initialize,
            ArgoState.INITIALIZING: self._initialize,
            ArgoState.INITIALIZED: self._start_migration,
            ArgoState.MIGRATION_SUBMITTING: self._recover_migration,
            ArgoState.MIGRATION_WAITING: self._wait_migration,
            ArgoState.PREPARING: self._prepare,
            ArgoState.SUBMITTING: self._submit,
            ArgoState.WAITING: self._drive_candidate,
            ArgoState.VERIFYING: self._drive_candidate,
            ArgoState.PROMOTING: self._drive_candidate,
            ArgoState.CHECKING: self._drive_candidate,
            ArgoState.RESTORE_SUBMITTING: self._submit_restore,
            ArgoState.RESTORE_WAITING: self._drive_restore,
            ArgoState.RESTORE_VERIFYING: self._drive_restore,
            ArgoState.RESTORE_PROMOTING: self._drive_restore,
            ArgoState.RESTORE_CHECKING: self._drive_restore,
            ArgoState.UNPUBLISHING: self._unpublish,
        }

    def _step(self, conn, row):
        rt = self.runtime
        if row["binding_fingerprint"] != rt.settings.fingerprint():
            raise ProviderConflict("Argo environment changed")
        request = ArgoDeploymentRequest.model_validate(row["request"])
        rt.settings.validate_request(request)
        tick = ArgoTick(conn, row, request, ReleaseKey(value=row["id"]), dict(row["checkpoint"]))
        if row["expired"]:
            self._goto(
                tick,
                "failed" if row["state"] == ArgoState.QUEUED else "uncertain",
                "Argo 작업 기한 초과. 현재 리소스를 확인해야 합니다.",
                error_code="deadline_exceeded",
            )
            return
        rt.connect()
        handler = self._handlers().get(row["state"])
        if handler is None:
            raise ProviderConflict("Unknown Argo state")
        handler(tick)

    def _initialize(self, tick: ArgoTick):
        provider = self.runtime.provider
        if tick.state == ArgoState.QUEUED:
            self._goto(tick, ArgoState.INITIALIZING, "비공개 후보 경로를 준비합니다.")
        before = provider.initialize(tick.request)
        tick.cp["before"] = before.model_dump() if before else None
        tick.cp["previous"] = self._verified_previous(tick, before) if before else None
        self._goto(tick, ArgoState.INITIALIZED, "기존 릴리즈를 기록했습니다.")

    def _verified_previous(self, tick: ArgoTick, before: Revision) -> dict:
        rt = self.runtime
        previous = tick.conn.execute(
            """SELECT * FROM releases WHERE id=%s AND application=%s AND environment_id=%s
               AND runtime=%s AND state='succeeded' AND binding_fingerprint=%s""",
            (
                before.release,
                tick.request.application,
                tick.request.environment_id,
                self.queue_runtime,
                tick.row["binding_fingerprint"],
            ),
        ).fetchone()
        tick.conn.commit()
        if not previous:
            raise ProviderConflict("Rollout has no verified previous release")
        old = ArgoDeploymentRequest.model_validate(previous["request"])
        rt.settings.validate_request(old)
        obs = rt.provider.observe(old, ReleaseKey(value=before.release), expected_uid=before.uid)
        if not obs.active or not obs.ready:
            raise ProviderConflict("Previous Rollout is not stable")
        return {"id": before.release, "request": previous["request"]}

    def _start_migration(self, tick: ArgoTick):
        if not tick.request.migration:
            self._goto(tick, ArgoState.PREPARING, "Rollout 갱신 준비.")
            return
        self._goto(tick, ArgoState.MIGRATION_SUBMITTING, "Migration 실행 의도 저장.")
        submission = self.runtime.provider.submit_migration(tick.request, tick.key)
        tick.cp["migration"] = submission.model_dump(mode="json")
        self._goto(tick, ArgoState.MIGRATION_WAITING, "Migration 완료 대기.")

    def _recover_migration(self, tick: ArgoTick):
        result = self.runtime.provider.reconcile_migration(tick.request, tick.key)
        if result is None:
            self._goto(
                tick,
                "uncertain",
                "Migration 결과 불명. 중복 실행하지 않습니다.",
                error_code="migration_result_unknown",
            )
            return
        tick.cp["migration"] = result.model_dump(mode="json")
        self._goto(tick, ArgoState.MIGRATION_WAITING, "기존 Migration 재조회.")

    def _wait_migration(self, tick: ArgoTick):
        result = self.runtime.provider.observe_migration(
            tick.request, tick.key, expected_uid=tick.cp["migration"]["operation_id"]
        )
        if result.phase == Phase.READY:
            self._goto(tick, ArgoState.PREPARING, "Migration 완료.")
        elif result.phase == Phase.FAILED:
            self._goto(
                tick,
                "failed",
                "Migration 실패. Rollout은 변경하지 않았습니다.",
                error_code=result.code,
            )
        else:
            self._wait(tick)

    def _prepare(self, tick: ArgoTick):
        provider = self.runtime.provider
        current = provider.read_rollout()
        revision = provider.revision(current) if current else None
        before = Revision.model_validate(tick.cp["before"]) if tick.cp["before"] else None
        if before and (revision is None or not revision.same_generation(before)):
            raise ProviderConflict("Rollout spec changed while preparing")
        if before is None and revision is not None:
            raise ProviderConflict("Unexpected Rollout appeared")
        tick.cp["expected"] = revision.model_dump() if revision else None
        self._goto(tick, ArgoState.SUBMITTING, "Rollout 생성·갱신 의도 저장.")

    def _submit(self, tick: ArgoTick):
        expected = tick.cp["expected"]
        revision = self.runtime.provider.submit(
            tick.request,
            tick.key,
            expected=Revision.model_validate(expected) if expected else None,
        )
        self._record_revision(tick, revision)
        self._goto(tick, ArgoState.WAITING, "Argo 후보를 기다립니다.")

    @staticmethod
    def _record_revision(tick: ArgoTick, revision: Revision) -> None:
        tick.cp["revision"] = revision.model_dump()
        tick.cp["samples"], tick.cp["attempts"] = 0, 0

    def _previous_request(self, tick: ArgoTick) -> tuple[ArgoDeploymentRequest, ReleaseKey]:
        old = ArgoDeploymentRequest.model_validate(tick.cp["previous"]["request"])
        self.runtime.settings.validate_request(old)
        return old, ReleaseKey(value=tick.cp["previous"]["id"])

    def _submit_restore(self, tick: ArgoTick):
        old, old_key = self._previous_request(tick)
        revision = self.runtime.provider.submit(
            old, old_key, expected=Revision.model_validate(tick.cp["restore_expected"])
        )
        self._record_revision(tick, revision)
        self._goto(tick, ArgoState.RESTORE_WAITING, "이전 템플릿 복원. DB는 되돌리지 않습니다.")

    def _unpublish(self, tick: ArgoTick):
        self.runtime.provider.unpublish(expected_uid=tick.cp["ingress_uid"])
        self._goto(
            tick,
            "uncertain",
            "첫 배포 공개 경로 제거. 실패한 Rollout을 점검하세요.",
            error_code="first_release_failed",
        )

    def _drive_candidate(self, tick: ArgoTick):
        self._drive(tick, tick.request, tick.key, restoring=False)

    def _drive_restore(self, tick: ArgoTick):
        old, old_key = self._previous_request(tick)
        self._drive(tick, old, old_key, restoring=True)

    def _drive(self, tick: ArgoTick, request, key, *, restoring: bool):
        obs = self.runtime.provider.observe(request, key, expected_uid=tick.cp["revision"]["uid"])
        drive = Drive(tick, request, key, obs, restoring)
        if obs.failed:
            self._failure(drive)
            return
        phase = DrivePhase(tick.state.removeprefix(RESTORE_PREFIX))
        {
            DrivePhase.WAITING: self._drive_waiting,
            DrivePhase.VERIFYING: self._drive_verifying,
            DrivePhase.PROMOTING: self._drive_promoting,
            DrivePhase.CHECKING: self._drive_checking,
        }[phase](drive)

    def _drive_waiting(self, drive: Drive):
        tick = drive.tick
        if not drive.obs.ready:
            self._wait(tick)
            return
        tick.cp["samples"], tick.cp["attempts"] = 0, 0
        self._goto(tick, drive.state(DrivePhase.VERIFYING), "비공개 preview를 검증합니다.")

    def _drive_verifying(self, drive: Drive):
        tick, rt = drive.tick, self.runtime
        probe_options = {}
        if rt.settings.preview_probe_mode == "job":
            prefix = RESTORE_PREFIX if drive.restoring else ""
            probe_id = f"{tick.row['id']}-{prefix}preview-{tick.cp.get('attempts', 0)}"
            probe_options = {"probe_id": probe_id}
        healthy = drive.obs.ready and rt.probe(
            drive.request, drive.key, production=False, **probe_options
        )
        if healthy is None:
            self._wait(tick)
            return
        self._record_sample(tick, healthy)
        if tick.cp["samples"] >= rt.settings.required_health_samples:
            self._goto(tick, drive.state(DrivePhase.PROMOTING), "후보 검증 통과. 승격 의도 저장.")
        elif tick.cp["attempts"] >= rt.settings.production_check_limit:
            self._failure(drive)
        else:
            self._wait(tick)

    def _drive_promoting(self, drive: Drive):
        tick, provider, obs = drive.tick, self.runtime.provider, drive.obs
        if not obs.ready:
            self._wait(tick)
        elif obs.active:
            tick.cp["ingress_uid"] = provider.publish()
            tick.cp["samples"], tick.cp["attempts"] = 0, 0
            self._goto(tick, drive.state(DrivePhase.CHECKING), "운영 HTTPS를 반복 확인합니다.")
        elif obs.paused:
            provider.promote(drive.request, drive.key, expected=obs.revision)
            self._wait(tick)
        else:
            self._wait(tick)

    def _drive_checking(self, drive: Drive):
        tick, rt, obs = drive.tick, self.runtime, drive.obs
        healthy = obs.ready and obs.active and rt.probe(drive.request, drive.key, production=True)
        self._record_sample(tick, healthy)
        if tick.cp["samples"] >= rt.settings.required_health_samples:
            self._goto(
                tick,
                "failed" if drive.restoring else "succeeded",
                "이전 버전 복구 확인." if drive.restoring else "운영 HTTPS·릴리즈 검증 완료.",
                error_code="release_failed_restored" if drive.restoring else None,
                url=f"https://{rt.settings.environment.hostname}",
            )
        elif tick.cp["attempts"] >= rt.settings.production_check_limit:
            self._failure(drive)
        else:
            self._wait(tick)

    @staticmethod
    def _record_sample(tick: ArgoTick, healthy: bool) -> None:
        tick.cp["samples"] = tick.cp.get("samples", 0) + 1 if healthy else 0
        tick.cp["attempts"] = tick.cp.get("attempts", 0) + 1

    def _failure(self, drive: Drive):
        tick, cp = drive.tick, drive.tick.cp
        if drive.restoring:
            self._goto(
                tick,
                "uncertain",
                "이전 버전 복구 검증 실패. 운영자 확인 필요.",
                error_code="restore_unverified",
            )
        elif cp["previous"]:
            cp["restore_expected"] = drive.obs.revision.model_dump()
            self._goto(tick, ArgoState.RESTORE_SUBMITTING, "실패. 이전 요청 복원 의도 저장.")
        elif cp.get("ingress_uid"):
            self._goto(tick, ArgoState.UNPUBLISHING, "첫 배포 실패. 공개 경로 제거 의도 저장.")
        else:
            self._goto(
                tick,
                "uncertain",
                "첫 후보 실패. 공개하지 않고 점검을 기다립니다.",
                error_code="first_candidate_failed",
            )
