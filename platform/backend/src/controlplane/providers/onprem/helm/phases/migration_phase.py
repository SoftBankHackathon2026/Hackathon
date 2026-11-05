from controlplane.providers.contracts import Phase, ProviderConflict
from controlplane.providers.onprem.helm.phases.base import HelmPhase, HelmTick
from controlplane.providers.onprem.helm.states import HelmState


class MigrationPhase(HelmPhase):
    def _on_migration_ready(self, tick: HelmTick):
        if not tick.request.migration:
            self._goto(tick, HelmState.APP_READY, "앱 Helm 배포 준비.")
            return
        provider, bound = self.runtime.migration(tick.request, tick.cp.get("data_values"))
        self._goto(tick, HelmState.MIGRATION_SUBMITTING, "Migration Job 실행 의도 저장.")
        submission = provider.submit_migration(bound, tick.key)
        tick.cp["migration"] = submission.model_dump(mode="json")
        self._goto(tick, HelmState.MIGRATION_WAITING, "Migration 완료 대기.")

    def _on_migration_submitting(self, tick: HelmTick):
        provider, bound = self.runtime.migration(tick.request, tick.cp.get("data_values"))
        result = provider.reconcile_migration(bound, tick.key)
        if result is None:
            raise ProviderConflict("Migration outcome unknown; no replay")
        tick.cp["migration"] = result.model_dump(mode="json")
        self._goto(tick, HelmState.MIGRATION_WAITING, "기존 Migration을 재조회합니다.")

    def _on_migration_waiting(self, tick: HelmTick):
        provider, bound = self.runtime.migration(tick.request, tick.cp.get("data_values"))
        result = provider.observe_migration(
            bound, tick.key, expected_uid=tick.cp["migration"]["operation_id"]
        )
        if result.phase == Phase.READY:
            self._goto(
                tick, HelmState.APP_READY, "Migration 완료. DB 스키마는 앱 롤백 대상이 아닙니다."
            )
        elif result.phase == Phase.FAILED:
            self._goto(
                tick,
                HelmState.FAILED,
                "Migration 실패. 앱 버전은 변경하지 않았습니다.",
                error_code="migration_failed",
            )
        else:
            self._wait(tick)
