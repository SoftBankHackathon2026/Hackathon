"""트래픽 관문 — 대상 쪽 가중치를 올려도 되는지 판정한다(설계 0.1 S1 G3).

두 신호를 본다. 대상 앱 health 연속 성공 수(외부 LB 경유, DB 를 읽는
경로)와 트래픽 봇의 프로브 오류율이다. 하나라도 잴 수 없으면 거부한다.

오류율은 봇 /metrics 의 `traffic_bot_requests_total{state=...}`(결과가 정해진
요청 수 게이지)를 창 앞뒤로 두 번 읽어, 그 사이 결과가 정해진 요청 중
failed·unknown 의 비율로 잰다.

  signals = gate.HttpTrafficSignals(health_url=..., bot_metrics_url=..., ...)
  decision = gate.traffic_gate(
      signals.target_health_successes(), signals.probe_error_rate(), params)
"""

import collections.abc
import dataclasses
import re
import time
import urllib.error
import urllib.request

from crosscloud import health_probe

_OUTCOME_LINE = re.compile(
    r'^traffic_bot_requests_total\{state="(\w+)"\}\s+([0-9.eE+-]+)\s*$'
)
_SETTLED = ("committed", "failed", "unknown")
_BAD = ("failed", "unknown")
_HTTP_TIMEOUT_SECONDS = 5
_HEALTH_INTERVAL_SECONDS = 1


@dataclasses.dataclass(frozen=True)
class GateParams:
    """관문 수치. 정책이 아니라 운영 입력이다.

    Attributes:
        health_consecutive_successes: 필요한 대상 health 연속 성공 수.
        max_probe_error_rate: 허용하는 봇 프로브 오류율 상한(0~1).
        health_deadline_seconds: health 연속 성공을 기다리는 상한.
        error_window_seconds: 오류율을 재는 창.
    """

    health_consecutive_successes: int
    max_probe_error_rate: float
    health_deadline_seconds: float
    error_window_seconds: float


@dataclasses.dataclass(frozen=True)
class GateDecision:
    """판정. 허용은 allowed 가 True 일 때뿐이다."""

    allowed: bool
    reasons: tuple[str, ...]


def traffic_gate(
    health_successes: int | None,
    probe_error_rate: float | None,
    params: GateParams,
) -> GateDecision:
    """두 신호로 대상 방향 가중치 증가를 판정한다. None 은 측정 불가다."""
    reasons = []
    needed = params.health_consecutive_successes
    if health_successes is None or health_successes < needed:
        reasons.append(f"대상 health 연속 성공 {health_successes}/{needed}")
    if probe_error_rate is None:
        reasons.append("프로브 오류율 측정 불가")
    elif probe_error_rate > params.max_probe_error_rate:
        reasons.append(
            f"프로브 오류율 {probe_error_rate:.4f} >"
            f" {params.max_probe_error_rate:.4f}"
        )
    return GateDecision(not reasons, tuple(reasons))


def parse_bot_outcomes(text: str) -> dict[str, float]:
    """봇 /metrics 본문에서 결과 상태별 요청 수를 뽑는다."""
    outcomes = {}
    for line in text.splitlines():
        match = _OUTCOME_LINE.match(line.strip())
        if match:
            outcomes[match.group(1)] = float(match.group(2))
    return outcomes


class HttpTrafficSignals:
    """HTTP 로 두 신호를 잰다. 대상 health 주소와 봇 /metrics 주소를 쓴다."""

    def __init__(
        self,
        *,
        health_url: str,
        bot_metrics_url: str,
        params: GateParams,
        clock: collections.abc.Callable[[], float] = time.monotonic,
        sleep: collections.abc.Callable[[float], None] = time.sleep,
    ) -> None:
        """잴 주소와 관문 수치를 받는다."""
        self._health_url = health_url
        self._metrics_url = bot_metrics_url
        self._params = params
        self._clock = clock
        self._sleep = sleep

    def target_health_successes(self) -> int:
        """대상 health 연속 성공 수(필요 수에 닿으면 멈춘다)."""
        return health_probe.consecutive_successes(
            lambda: health_probe.is_healthy(
                self._health_url, _HTTP_TIMEOUT_SECONDS
            ),
            needed=self._params.health_consecutive_successes,
            deadline_seconds=self._params.health_deadline_seconds,
            interval=_HEALTH_INTERVAL_SECONDS,
            clock=self._clock,
            sleep=self._sleep,
        )

    def probe_error_rate(self) -> float | None:
        """창 동안의 봇 오류율. 읽기 실패·새 요청 없음이면 None."""
        before = self._read_outcomes()
        if before is None:
            return None
        self._sleep(self._params.error_window_seconds)
        after = self._read_outcomes()
        if after is None:
            return None
        settled = sum(after[s] - before[s] for s in _SETTLED)
        if settled <= 0:
            return None
        return sum(after[s] - before[s] for s in _BAD) / settled

    def _read_outcomes(self) -> dict[str, float] | None:
        try:
            with urllib.request.urlopen(
                self._metrics_url, timeout=_HTTP_TIMEOUT_SECONDS
            ) as response:
                text = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError):
            return None
        outcomes = parse_bot_outcomes(text)
        if not all(state in outcomes for state in _SETTLED):
            return None
        return outcomes
