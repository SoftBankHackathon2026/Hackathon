from controlplane.domain.models import Strategy
from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.onprem.helm.models import HelmPlan, is_deployed_at
from controlplane.providers.onprem.helm.observation import RolloutState
from controlplane.providers.onprem.helm.phases.base import HelmPhase, HelmTick
from controlplane.providers.onprem.helm.plan import fingerprint
from controlplane.providers.onprem.helm.states import HelmState, ReleaseKind

STARTUP_FAILURE_LIMIT = 3


def _baseline_revision(cp: dict) -> int | None:
    fallback = (cp.get("previous") or cp.get("retry_base") or {}).get("receipt")
    return (cp.get("app_receipt") or fallback or {}).get("revision")


def _known_rollout_uid(cp: dict) -> str | None:
    return cp.get("rollout_uid") or (cp.get("previous") or cp.get("retry_base") or {}).get("uid")


class AppPhase(HelmPhase):
    def _on_app_ready(self, tick: HelmTick):
        self._submit_app(tick, publishing=False)

    def _on_publish_ready(self, tick: HelmTick):
        self._submit_app(tick, publishing=True)

    def _migrate_strategy_first(self, tick: HelmTick, plan: HelmPlan) -> bool:
        cp = tick.cp
        if not cp.get("previous") or cp.get("strategy_migrated"):
            return False
        old = HelmPlan.model_validate(cp["previous"]["plan"])
        if old.strategy_type == plan.strategy_type:
            return False
        strategy = plan.values["strategy"]
        plan.values = old.model_copy(deep=True).values
        plan.values["strategy"] = strategy
        plan.fingerprint = fingerprint(plan.model_dump(exclude={"fingerprint"}))
        return True

    @staticmethod
    def _state_after_apply(tick: HelmTick, *, publishing: bool, migrating: bool) -> HelmState:
        if migrating:
            return HelmState.STRATEGY_WAITING
        if publishing:
            return HelmState.CHECKING
        if tick.request.strategy == Strategy.CANARY:
            return HelmState.CANARY_WAITING
        return HelmState.APP_WAITING

    def _submit_app(self, tick: HelmTick, *, publishing: bool):
        rt, cp = self.runtime, tick.cp
        published = bool(cp["previous"]) or publishing
        plan = rt.app_plan(tick.request, tick.key, cp.get("data_values"), published)
        previous_revision = _baseline_revision(cp)
        if publishing and cp["app_plan"]["values"]["route"]["enabled"]:
            self._goto(tick, HelmState.CHECKING, "공개 HTTPS·릴리즈 응답 확인.")
            return
        migrating = not publishing and self._migrate_strategy_first(tick, plan)
        cp["app_plan"] = plan.model_dump(mode="json")
        cp["expected_revision"] = previous_revision
        after_apply = self._state_after_apply(tick, publishing=publishing, migrating=migrating)
        cp["after_apply"] = after_apply.value
        self._goto(tick, HelmState.APP_SUBMITTING, "Helm 앱 변경 의도 저장.")
        receipt = rt.app.apply(plan, expected_revision=previous_revision)
        cp["app_receipt"] = receipt.model_dump(mode="json")
        self._goto(tick, after_apply, "Helm revision 기록. Argo·응답을 확인합니다.")

    def _on_app_submitting(self, tick: HelmTick):
        cp = tick.cp
        self._recover_apply(
            tick, ReleaseKind.APP, (cp["expected_revision"] or 0) + 1, cp["after_apply"]
        )

    def _record_rollout(self, cp: dict, obs: RolloutState) -> None:
        cp["rollout_uid"] = obs.uid
        cp["rollout_generation"] = obs.generation
        if obs.traffic_uid:
            self._record_traffic_uid(cp, obs)

    @staticmethod
    def _record_traffic_uid(cp: dict, obs: RolloutState) -> None:
        if cp.get("traffic_uid") and cp["traffic_uid"] != obs.traffic_uid:
            raise ProviderConflict("Canary traffic resource was replaced")
        cp["traffic_uid"] = obs.traffic_uid

    def _report_startup_issue(self, tick: HelmTick, issue: str | None) -> bool:
        cp = tick.cp
        if not issue:
            cp["startup_failure_samples"] = 0
            cp.pop("startup_issue", None)
            return False
        cp["startup_failure_samples"] = cp.get("startup_failure_samples", 0) + 1
        if cp.get("startup_issue") == issue:
            return False
        cp["startup_issue"] = issue
        self._goto(tick, tick.state, issue)
        return True

    def _on_rollout_waiting(self, tick: HelmTick):
        rt, cp = self.runtime, tick.cp
        plan = HelmPlan.model_validate(cp["app_plan"])
        obs = rt.observe(plan, _known_rollout_uid(cp))
        if obs:
            self._record_rollout(cp, obs)
        issue = obs.startup_issue if obs else None
        if self._report_startup_issue(tick, issue):
            return
        if issue and cp["startup_failure_samples"] >= STARTUP_FAILURE_LIMIT:
            self._failure(tick, "app_startup_failed")
        elif obs and obs.failed:
            self._failure(tick, "candidate_failed")
        elif obs and obs.ready:
            self._on_rollout_ready(tick, plan)
        else:
            self._report_canary_progress(tick, obs)

    def _on_rollout_ready(self, tick: HelmTick, plan: HelmPlan):
        if tick.state != HelmState.STRATEGY_WAITING:
            self._run_candidate_test(tick, ReleaseKind.APP, HelmState.PUBLISH_READY)
            return
        if not self.runtime.probe(plan):
            self._wait(tick)
            return
        tick.cp["strategy_migrated"] = True
        tick.cp.pop("traffic_uid", None)
        self._goto(tick, HelmState.APP_READY, "기존 버전으로 전략 변경 완료. 새 버전을 배포합니다.")

    def _report_canary_progress(self, tick: HelmTick, obs: RolloutState | None):
        cp = tick.cp
        if obs and obs.observed and tick.state == HelmState.CANARY_WAITING:
            progress = {"step": obs.step_index, "canary_weight": obs.canary_weight}
            if progress != cp.get("canary_progress"):
                cp["canary_progress"] = progress
                self._goto(tick, tick.state, self._canary_message(cp, obs))
                return
        self._wait(tick)

    @staticmethod
    def _canary_message(cp: dict, obs: RolloutState) -> str:
        if cp.get("previous") and obs.canary_weight is not None:
            return f"카나리 새 버전 트래픽 {obs.canary_weight}%. 단계별 검사·대기 중입니다."
        return "첫 배포: 비교할 기존 버전 없이 후보를 준비합니다."

    def _on_checking(self, tick: HelmTick):
        rt, cp = self.runtime, tick.cp
        restoring = tick.state == HelmState.RESTORE_CHECKING
        plan = HelmPlan.model_validate(cp["previous"]["plan"] if restoring else cp["app_plan"])
        obs = rt.observe(plan, cp["rollout_uid"])
        if (
            obs
            and obs.traffic_uid
            and cp.get("traffic_uid")
            and cp["traffic_uid"] != obs.traffic_uid
        ):
            raise ProviderConflict("Canary traffic resource was replaced")
        if restoring and (not obs or not obs.ready):
            self._wait(tick)
            return
        healthy = obs and obs.ready and rt.probe(plan)
        cp["samples"] = cp.get("samples", 0) + 1 if healthy else 0
        cp["attempts"] = cp.get("attempts", 0) + 1
        if cp["samples"] >= rt.settings.required_health_samples:
            self._conclude_checks(tick, restoring)
        elif cp["attempts"] >= rt.settings.production_check_limit:
            if restoring:
                raise ProviderConflict("Rollback verification failed")
            self._failure(tick, "public_health_failed")
        else:
            self._wait(tick)

    def _conclude_checks(self, tick: HelmTick, restoring: bool):
        cp = tick.cp
        if restoring:
            cp["app_plan"] = cp["previous"]["plan"]
            cp["app_receipt"] = cp["restore_receipt"]
        self._goto(
            tick,
            HelmState.FAILED if restoring else HelmState.SUCCEEDED,
            "이전 앱 복구 확인. DB 유지."
            if restoring
            else "Helm 앱·DB 연결 및 공개 응답 검증 완료.",
            error_code="release_failed_restored" if restoring else None,
            url="https://" + self.runtime.settings.environment.hostname,
        )

    def _on_restore_submitting(self, tick: HelmTick):
        rt, cp = self.runtime, tick.cp
        old = cp["previous"]
        plan = HelmPlan.model_validate(old["plan"])
        expected = cp["restore_from"] + 1
        if (
            is_deployed_at(rt.app.current(plan.release), expected)
            and rt.app.values(plan.release, expected) == plan.values
        ):
            restored = rt.app.observe_receipt(plan, expected)
            cp["restore_receipt"] = restored.model_dump(mode="json")
            self._goto(tick, HelmState.RESTORE_CHECKING, "복구 revision 확인. 공개 응답 검증.")
        else:
            raise ProviderConflict("Rollback outcome requires review; no duplicate write")
