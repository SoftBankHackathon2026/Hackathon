"""Helm app/data workflow with committed write intents and conservative reconciliation."""

import time

from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.providers.contracts import ProviderConflict, ReleaseKey
from controlplane.providers.onprem.helm import delete
from controlplane.providers.onprem.helm.phases.app_phase import AppPhase
from controlplane.providers.onprem.helm.phases.base import HelmTick
from controlplane.providers.onprem.helm.phases.data_phase import DataPhase
from controlplane.providers.onprem.helm.phases.intake import IntakePhase
from controlplane.providers.onprem.helm.phases.migration_phase import MigrationPhase
from controlplane.providers.onprem.helm.states import HelmState

RECHECK_INTERVAL_SECONDS = 30
REQUIRED_CANDIDATE_KEYS = ("app_plan", "app_receipt", "rollout_uid")

FIRST_FAILURE_SQL = """SELECT * FROM releases WHERE application=%s AND environment_id=%s
    AND runtime='onprem_helm' AND state='uncertain' AND archived IS NULL
    AND error_code IN ('candidate_failed','helm_deadline')
    AND binding_fingerprint=%s ORDER BY created_at DESC LIMIT 1"""


class HelmWorker(IntakePhase, DataPhase, MigrationPhase, AppPhase):
    queue_runtime = Runtime.ONPREM_HELM

    def __init__(self, store, runtime):
        super().__init__(store, runtime)
        self._next_recheck = 0.0

    def tick(self):
        if time.monotonic() >= self._next_recheck:
            self._next_recheck = time.monotonic() + RECHECK_INTERVAL_SECONDS
            self._reconcile_first_failure()
        super().tick()

    def _reconcile_first_failure(self):
        rt, env = self.runtime, self.runtime.settings.environment
        with self.store.worker_lock() as conn:
            if conn is None:
                return
            row = conn.execute(
                FIRST_FAILURE_SQL, (env.application, env.environment_id, rt.settings.fingerprint())
            ).fetchone()
            conn.commit()
            if not row:
                return
            cp = dict(row["checkpoint"])
            if not all(cp.get(k) for k in REQUIRED_CANDIDATE_KEYS):
                return
            try:
                rt.connect()
                _, _, obs = self._recorded_candidate(row, rt.app.current(cp["app_plan"]["release"]))
            except Exception:
                return
            if obs.failed or obs.startup_issue:
                if obs.startup_issue:
                    cp["startup_issue"] = obs.startup_issue
                tick = self._tick_for(conn, row, cp)
                self._failure(tick, "candidate_failed")

    def _delete(self, deletion):
        delete.step(deletion, self.runtime)

    @staticmethod
    def _tick_for(conn, row, cp) -> HelmTick:
        return HelmTick(
            conn=conn,
            row=row,
            cp=cp,
            request=DeploymentRequest.model_validate(row["request"]),
            key=ReleaseKey(value=row["id"]),
        )

    def _handlers(self):
        return {
            HelmState.QUEUED: self._on_queued,
            HelmState.DATA_PREPARING: self._on_data_preparing,
            HelmState.DATA_SUBMITTING: self._on_data_submitting,
            HelmState.DATA_WAITING: self._on_data_waiting,
            HelmState.DATA_TESTING: self._on_testing,
            HelmState.APP_TESTING: self._on_testing,
            HelmState.MIGRATION_READY: self._on_migration_ready,
            HelmState.MIGRATION_SUBMITTING: self._on_migration_submitting,
            HelmState.MIGRATION_WAITING: self._on_migration_waiting,
            HelmState.APP_READY: self._on_app_ready,
            HelmState.PUBLISH_READY: self._on_publish_ready,
            HelmState.APP_SUBMITTING: self._on_app_submitting,
            HelmState.APP_WAITING: self._on_rollout_waiting,
            HelmState.CANARY_WAITING: self._on_rollout_waiting,
            HelmState.STRATEGY_WAITING: self._on_rollout_waiting,
            HelmState.CHECKING: self._on_checking,
            HelmState.RESTORE_CHECKING: self._on_checking,
            HelmState.RESTORE_SUBMITTING: self._on_restore_submitting,
        }

    def _step(self, conn, row):
        rt = self.runtime
        if row["binding_fingerprint"] != rt.settings.fingerprint():
            raise ProviderConflict("Helm binding changed")
        request = DeploymentRequest.model_validate(row["request"])
        rt.settings.validate_request(request)
        tick = HelmTick(conn, row, dict(row["checkpoint"]), request, ReleaseKey(value=row["id"]))
        if row["expired"]:
            self._goto(
                tick,
                HelmState.UNCERTAIN,
                "Helm 작업 기한 초과. DB는 유지합니다.",
                error_code="helm_deadline",
            )
            return
        rt.connect()
        handler = self._handlers().get(row["state"])
        if handler is None:
            raise ProviderConflict("Unknown Helm state")
        handler(tick)
