"""운영자가 실행하는 트래픽 명령 — 가중치 이동·원본 복원·상태 조회.

명령은 shift-traffic · restore-source-routing · routing-status 다(설계 4.16
(iii)).

원가연 job 이 READY(종료 코드 3)인 동안, `run --approve` 전에 대상 쪽 가중치를
10→50→100 으로 옮긴다. 취소는 restore-source-routing(원본 100)이다.

  * 가중치를 바꾸는 명령은 원가연 cloud.lock 을 쥔 채 실행한다(`run` 과
    직렬화). 못 잡으면 거부한다.
  * 단계 경계는 cutover_job.routing_change_allowed 가 정한다(FENCING 부터
    거부, pause 미반영 FAILED(FENCING)만 예외).
  * 대상 쪽으로 올릴 때만 관문(gate.traffic_gate)을 거친다.
  * 변경은 INSYNC 일 때만 applied 다. 기한을 넘기면 unknown 이고 운영자가
    routing-status 로 확인한다(원장 없음).
"""

import collections.abc
import dataclasses
import json
import pathlib
import urllib.error
import urllib.request
from typing import Any, Literal

from botocore import exceptions

from crosscloud import cutover_job, model
from crosscloud.traffic import gate, route53

_INSYNC_INTERVAL_SECONDS = 5
_ADAPTER_TIMEOUT_SECONDS = 5


@dataclasses.dataclass(frozen=True)
class RoutingStatus:
    """가중 레코드와 job 단계. 읽지 못한 값은 None 이다."""

    zone_id: str
    record_name: str
    source_weight: int
    target_weight: int
    ttl_seconds: int | None
    last_change_id: str | None
    last_change_insync: bool | None
    stage: cutover_job.CutoverStage | None
    failed_stage: cutover_job.CutoverStage | None


@dataclasses.dataclass(frozen=True)
class TrafficResult:
    """명령 한 단계의 결과. unknown 은 변경을 보냈지만 INSYNC 를 못 본 것."""

    outcome: Literal["applied", "rejected", "unknown"]
    reason: str
    status: RoutingStatus | None


class TrafficCommands:
    """한 job·한 레코드 쌍에 대한 트래픽 명령."""

    def __init__(
        self,
        *,
        route: route53.WeightedRoute,
        change_record: route53.ChangeRecord,
        source: model.Cloud,
        target: model.Cloud,
        migration_state_dir: pathlib.Path,
        job_id: str,
        read_adapter_state: collections.abc.Callable[[], str | None],
        signals: gate.HttpTrafficSignals,
        gate_params: gate.GateParams,
        insync_timeout_seconds: float,
    ) -> None:
        """명령이 쓸 레코드·job·신호를 받는다."""
        self._route = route
        self._changes = change_record
        self._source = source
        self._target = target
        self._state_dir = migration_state_dir
        self._job_id = job_id
        self._read_adapter_state = read_adapter_state
        self._signals = signals
        self._gate_params = gate_params
        self._insync_timeout = insync_timeout_seconds

    def shift(self, weights: tuple[int, ...]) -> tuple[TrafficResult, ...]:
        """대상 가중치를 차례로 맞춘다. applied 가 아니면 거기서 멈춘다.

        Raises:
            ValueError: 가중치가 비었거나 0~100 정수가 아닐 때.
        """
        if not weights or not all(
            isinstance(w, int) and 0 <= w <= 100 for w in weights
        ):
            raise ValueError(f"가중치는 0~100 정수여야 한다: {weights}")
        results = []
        for weight in weights:
            result = self._locked_apply(weight)
            results.append(result)
            if result.outcome != "applied":
                break
        return tuple(results)

    def restore(self) -> TrafficResult:
        """원본 100 으로 되돌린다(승인 전 취소 경로). 관문은 없다."""
        return self._locked_apply(0)

    def status(self) -> RoutingStatus:
        """외부 변경 없이 가중치·마지막 변경·job 단계를 읽는다."""
        snapshot = self._route.read()
        change_id = self._changes.load()
        try:
            job = cutover_job.read_cutover_status(self._state_dir, self._job_id)
            stage, failed_stage = job.stage, job.failed_stage
        except cutover_job.StatusUnreadableError:
            stage, failed_stage = None, None
        return RoutingStatus(
            zone_id=self._route.zone_id,
            record_name=self._route.record_name,
            source_weight=snapshot.weights[self._source],
            target_weight=snapshot.weights[self._target],
            ttl_seconds=snapshot.ttl_seconds,
            last_change_id=change_id,
            last_change_insync=(
                None
                if change_id is None
                else self._route.change_insync(change_id)
            ),
            stage=stage,
            failed_stage=failed_stage,
        )

    def _locked_apply(self, target_weight: int) -> TrafficResult:
        try:
            with cutover_job.migration_job_lock(self._state_dir):
                return self._apply(target_weight)
        except (
            cutover_job.JobBusyError,
            cutover_job.StatusUnreadableError,
        ) as e:
            return TrafficResult("rejected", str(e), None)

    def _apply(self, target_weight: int) -> TrafficResult:
        job = cutover_job.read_cutover_status(self._state_dir, self._job_id)
        adapter_state = self._read_adapter_state()
        if not cutover_job.routing_change_allowed(job, adapter_state):
            return self._result(
                "rejected",
                f"job 단계 {job.stage}(failed_stage={job.failed_stage},"
                f" adapter={adapter_state})에서는 라우팅을 바꾸지 않는다",
            )
        current = self._route.read().weights[self._target]
        if target_weight == current:
            return self._result("applied", f"이미 대상 {target_weight}")
        if target_weight > current:
            decision = gate.traffic_gate(
                self._signals.target_health_successes(),
                self._signals.probe_error_rate(),
                self._gate_params,
            )
            if not decision.allowed:
                return self._result("rejected", "; ".join(decision.reasons))
        return self._change(target_weight)

    def _change(self, target_weight: int) -> TrafficResult:
        weights = {
            self._target: target_weight,
            self._source: 100 - target_weight,
        }
        try:
            change_id = self._route.set_weights(weights)
        except exceptions.ClientError as error:
            code = error.response.get("Error", {}).get("Code", "unknown")
            return self._result("rejected", f"Route 53 거절: {code}")
        except exceptions.BotoCoreError as error:
            return self._result(
                "unknown",
                f"Route 53 호출 결과 불명({type(error).__name__}) —"
                " routing-status 로 확인",
            )
        self._changes.save(change_id)
        if not self._route.wait_insync(
            change_id,
            timeout_seconds=self._insync_timeout,
            interval=_INSYNC_INTERVAL_SECONDS,
        ):
            return self._result(
                "unknown",
                f"{change_id} INSYNC 확인 시간 초과 — routing-status 로 확인",
            )
        return self._result("applied", f"대상 {target_weight} ({change_id})")

    def _result(self, outcome: str, reason: str) -> TrafficResult:
        return TrafficResult(outcome, reason, self.status())


def read_adapter_state(control_url: str) -> str | None:
    """제어 어댑터 GET /status 의 state. 못 읽으면 None."""
    try:
        with urllib.request.urlopen(
            control_url.rstrip("/") + "/status",
            timeout=_ADAPTER_TIMEOUT_SECONDS,
        ) as response:
            body: Any = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("state"), str):
        return None
    return body["state"]
