from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.onprem.helm.journal import (
    latest_data,
    latest_retryable_app,
    latest_verified_app,
)
from controlplane.providers.onprem.helm.models import HelmPlan, HelmReceipt, is_deployed_at
from controlplane.providers.onprem.helm.observation import RolloutState
from controlplane.providers.onprem.helm.phases.base import HelmPhase, HelmTick
from controlplane.providers.onprem.helm.plan import release_name
from controlplane.providers.onprem.helm.states import HelmState

CANARY_CHART_TYPE = "canary"
BLUE_GREEN_CHART_TYPE = "blueGreen"


class IntakePhase(HelmPhase):
    def _recorded_candidate(self, row, current) -> tuple[HelmPlan, HelmReceipt, RolloutState]:
        rt, saved = self.runtime, row["checkpoint"]
        plan = HelmPlan.model_validate(saved["app_plan"])
        receipt = HelmReceipt.model_validate(saved["app_receipt"])
        rt.verify_previous_binding(row["binding_fingerprint"], plan)
        if (
            saved.get("previous")
            or plan.route_enabled
            or not saved.get("rollout_uid")
            or not is_deployed_at(current, receipt.revision)
            or rt.app.values(plan.release, receipt.revision) != plan.values
        ):
            raise ProviderConflict("First candidate differs from recorded private revision")
        obs = rt.observe(plan, saved["rollout_uid"])
        if not obs or obs.generation != saved["rollout_generation"]:
            raise ProviderConflict("First candidate generation changed")
        return plan, receipt, obs

    def _verified_previous(self, tick: HelmTick, previous, current, name: str) -> None:
        rt, saved = self.runtime, previous["checkpoint"]
        receipt = HelmReceipt.model_validate(saved["app_receipt"])
        plan = HelmPlan.model_validate(saved["app_plan"])
        rt.verify_previous_binding(previous["binding_fingerprint"], plan)
        if (
            not is_deployed_at(current, receipt.revision)
            or rt.app.values(name, receipt.revision) != plan.values
        ):
            raise ProviderConflict("Helm revision differs from verified journal")
        obs = rt.observe(plan, saved["rollout_uid"])
        if not obs.ready:
            raise ProviderConflict("Previous app is not healthy")
        wanted = CANARY_CHART_TYPE if tick.request.strategy == "canary" else BLUE_GREEN_CHART_TYPE
        if plan.strategy_type != wanted and not rt.settings.app_chart.supports_strategy_switch:
            raise ProviderConflict("Existing app strategy cannot be changed")
        tick.cp["previous"] = {
            "plan": plan.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json"),
            "uid": obs.uid,
        }

    def _link_existing_release(self, tick: HelmTick, current, name: str) -> None:
        rt = self.runtime
        previous = latest_verified_app(tick.conn, rt.settings)
        if previous:
            tick.conn.commit()
            self._verified_previous(tick, previous, current, name)
            return
        retry = latest_retryable_app(tick.conn, rt.settings)
        tick.conn.commit()
        if not retry:
            raise ProviderConflict("No verified Helm revision to update")
        _, receipt, obs = self._recorded_candidate(retry, current)
        tick.cp["retry_base"] = {"receipt": receipt.model_dump(mode="json"), "uid": obs.uid}

    def _on_queued(self, tick: HelmTick):
        rt, cp = self.runtime, tick.cp
        cp["chart_source_commit"] = rt.settings.chart_source_commit
        cp["artifacts"] = {
            name: getattr(rt.settings, name).model_dump(mode="json", exclude={"path"})
            for name in ("app_chart", "data_chart")
        }
        rt.verify_hostname()
        name = release_name(tick.request.application, tick.request.environment_id)
        current = rt.app.current(name)
        cp["previous"] = None
        if current is not None:
            self._link_existing_release(tick, current, name)
        prior = latest_data(tick.conn, rt.settings)
        tick.conn.commit()
        cp["data_values"] = prior["checkpoint"]["data_values"] if prior else None
        cp["data_uids"] = prior["checkpoint"].get("data_uids", {}) if prior else {}
        cp["data_intent"] = tick.row["id"]
        self._goto(tick, HelmState.DATA_PREPARING, "DB·캐시 준비 의도를 저장했습니다.")
