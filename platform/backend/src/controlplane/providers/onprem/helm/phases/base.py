from dataclasses import dataclass
from typing import Any

from controlplane.domain.models import DeploymentRequest
from controlplane.execution.reconciler import ReleaseReconciler
from controlplane.providers.contracts import ProviderConflict, ReleaseKey
from controlplane.providers.onprem.helm.client import HelmClient
from controlplane.providers.onprem.helm.models import HelmPlan, HelmReceipt, is_deployed_at
from controlplane.providers.onprem.helm.states import HelmState, ReleaseKind


@dataclass
class HelmTick:
    conn: Any
    row: dict
    cp: dict
    request: DeploymentRequest
    key: ReleaseKey

    @property
    def state(self) -> str:
        return self.row["state"]


class HelmPhase(ReleaseReconciler):
    def _goto(self, tick: HelmTick, state, message, **kwargs):
        self._move(tick.conn, tick.row, state, message, tick.cp, **kwargs)

    def _wait(self, tick: HelmTick):
        self._poll(tick.conn, tick.row, tick.cp)

    def _client(self, kind: ReleaseKind) -> HelmClient:
        return self.runtime.data if kind == ReleaseKind.DATA else self.runtime.app

    def _recover_apply(self, tick: HelmTick, kind: ReleaseKind, revision: int, next_state):
        client = self._client(kind)
        plan = HelmPlan.model_validate(tick.cp[kind + "_plan"])
        if (
            not is_deployed_at(client.current(plan.release), revision)
            or client.values(plan.release, revision) != plan.values
        ):
            raise ProviderConflict("Helm write outcome not proven; refusing duplicate upgrade")
        tick.cp[kind + "_receipt"] = client.observe_receipt(plan, revision).model_dump(mode="json")
        self._goto(tick, next_state, "기존 Helm 실행 결과를 대조했습니다.")

    def _run_candidate_test(self, tick: HelmTick, kind: ReleaseKind, next_state):
        cp = tick.cp
        receipt = HelmReceipt.model_validate(cp[kind + "_receipt"])
        _, previous_uid = self.runtime.test_result(receipt)
        cp[kind + "_previous_test_uid"] = previous_uid
        cp.pop(kind + "_test_uid", None)
        testing = HelmState.DATA_TESTING if kind == ReleaseKind.DATA else HelmState.APP_TESTING
        self._goto(
            tick,
            testing,
            "인증된 연결 검사." if kind == ReleaseKind.DATA else "비공개 앱 후보 검사.",
        )
        self._client(kind).test(receipt)
        result, uid = self.runtime.test_result(receipt, previous_uid=previous_uid)
        cp[kind + "_test_uid"] = uid
        if result is not True:
            raise ProviderConflict("Helm test success was not observed")
        self._goto(tick, next_state, "검사 Job 성공을 확인했습니다.")

    def _failure(self, tick: HelmTick, code: str):
        cp = tick.cp
        if not cp.get("app_receipt"):
            self._goto(
                tick,
                HelmState.FAILED,
                "데이터 검사 실패. 기존 앱과 DB는 유지합니다.",
                error_code=code,
            )
            return
        if not cp.get("previous"):
            self._fail_first_candidate(tick, code)
            return
        self._restore_previous(tick)

    def _fail_first_candidate(self, tick: HelmTick, code: str):
        cp = tick.cp
        retryable = bool(cp.get("rollout_uid")) and not cp["app_plan"]["values"]["route"]["enabled"]
        cp["retryable_first_deploy"] = retryable
        if retryable:
            state = HelmState.FAILED
            message = (
                cp.get("startup_issue", "새 버전의 상태 확인에 실패했습니다.")
                + " 첫 배포에 실패했습니다. DB는 보존합니다. 설정을 수정한 뒤 다시 배포하세요."
            )
        else:
            state = HelmState.UNCERTAIN
            message = "첫 후보 실패. 데이터는 보존하고 자동 재설치하지 않습니다."
        self._goto(tick, state, message, error_code=code)

    def _restore_previous(self, tick: HelmTick):
        cp = tick.cp
        old = cp["previous"]
        plan = HelmPlan.model_validate(old["plan"])
        receipt = HelmReceipt.model_validate(old["receipt"])
        cp["restore_from"] = cp["app_receipt"]["revision"]
        cp["samples"], cp["attempts"] = 0, 0
        cp.pop("traffic_uid", None)
        self._goto(
            tick, HelmState.RESTORE_SUBMITTING, "검증된 이전 Helm revision 복원 의도 저장. DB 유지."
        )
        cp["restore_receipt"] = self.runtime.rollback_app(
            plan, receipt, expected_revision=cp["restore_from"]
        ).model_dump(mode="json")
        self._goto(tick, HelmState.RESTORE_CHECKING, "앱 롤백 결과를 확인합니다.")
