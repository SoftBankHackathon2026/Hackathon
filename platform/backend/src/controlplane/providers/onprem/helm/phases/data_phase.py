from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.onprem.helm import data
from controlplane.providers.onprem.helm.models import HelmPlan, HelmReceipt
from controlplane.providers.onprem.helm.phases.base import HelmPhase, HelmTick
from controlplane.providers.onprem.helm.states import HelmState, ReleaseKind


class DataPhase(HelmPhase):
    def _on_data_preparing(self, tick: HelmTick):
        rt, cp, request = self.runtime, tick.cp, tick.request
        if request.resources.database == "none" and not request.resources.redis:
            if cp.get("data_values"):
                raise ProviderConflict("Removing attached data requires an explicit operation")
            self._goto(tick, HelmState.MIGRATION_READY, "데이터 서비스 없음.")
            return
        cp["data_values"] = data.prepare(
            rt.clients, rt.settings.environment, request, cp["data_intent"], cp.get("data_values")
        )
        plan = data.plan(cp["data_values"], rt.settings.data_chart)
        cp["data_plan"] = plan.model_dump(mode="json")
        current = rt.data.current(plan.release)
        if current is not None:
            self._adopt_existing_data(tick, plan, current)
            return
        self._goto(tick, HelmState.DATA_SUBMITTING, "데이터 Helm 설치 의도 저장.")
        receipt = rt.data.apply(plan, expected_revision=None)
        cp["data_receipt"] = receipt.model_dump(mode="json")
        self._goto(tick, HelmState.DATA_WAITING, "PostgreSQL·Redis 준비를 기다립니다.")

    def _adopt_existing_data(self, tick: HelmTick, plan: HelmPlan, current) -> None:
        rt = self.runtime
        if (
            current.status != "deployed"
            or rt.data.values(plan.release, current.revision) != plan.values
        ):
            raise ProviderConflict("Existing data Helm release differs")
        receipt = rt.data.observe_receipt(plan, current.revision)
        tick.cp["data_receipt"] = receipt.model_dump(mode="json")
        self._goto(tick, HelmState.DATA_WAITING, "기존 DB·PVC·Secret을 재사용합니다.")

    def _on_data_submitting(self, tick: HelmTick):
        self._recover_apply(tick, ReleaseKind.DATA, 1, HelmState.DATA_WAITING)

    def _on_data_waiting(self, tick: HelmTick):
        rt, cp = self.runtime, tick.cp
        ready, cp["data_uids"] = data.observe(
            rt.clients, HelmPlan.model_validate(cp["data_plan"]), cp.get("data_uids", {})
        )
        if ready:
            self._run_candidate_test(tick, ReleaseKind.DATA, HelmState.MIGRATION_READY)
        else:
            self._wait(tick)

    def _on_testing(self, tick: HelmTick):
        cp = tick.cp
        kind = ReleaseKind(tick.state.split("_")[0])
        receipt = HelmReceipt.model_validate(cp[kind + "_receipt"])
        result, uid = self.runtime.test_result(
            receipt, cp.get(kind + "_test_uid"), cp.get(kind + "_previous_test_uid")
        )
        if uid:
            cp[kind + "_test_uid"] = uid
        if result is True:
            next_state = (
                HelmState.MIGRATION_READY if kind == ReleaseKind.DATA else HelmState.PUBLISH_READY
            )
            self._goto(tick, next_state, "인증·후보 검사 완료.")
        elif result is False:
            self._failure(tick, "helm_test_failed")
        else:
            self._wait(tick)
