import http_fixture
import pytest

from crosscloud.traffic import gate

_PARAMS = gate.GateParams(
    health_consecutive_successes=3,
    max_probe_error_rate=0.05,
    health_deadline_seconds=25,
    error_window_seconds=30,
)

_METRICS_BEFORE = b"""# HELP traffic_bot_requests_total Durable request outcomes
# TYPE traffic_bot_requests_total gauge
traffic_bot_requests_total{state="pending"} 2
traffic_bot_requests_total{state="committed"} 100
traffic_bot_requests_total{state="failed"} 1
traffic_bot_requests_total{state="unknown"} 0
traffic_bot_paused 0
"""
_METRICS_AFTER = b"""traffic_bot_requests_total{state="pending"} 1
traffic_bot_requests_total{state="committed"} 190
traffic_bot_requests_total{state="failed"} 3
traffic_bot_requests_total{state="unknown"} 1
"""


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_gate_allows_healthy_target_with_low_error_rate():
    decision = gate.traffic_gate(3, 0.01, _PARAMS)

    assert decision == gate.GateDecision(True, ())


@pytest.mark.parametrize(
    ("health", "error_rate", "reason"),
    [
        (None, 0.0, "health"),
        (2, 0.0, "health"),
        (3, None, "오류율"),
        (3, 0.06, "오류율"),
    ],
)
def test_gate_denies_unmeasured_or_failing_signals(health, error_rate, reason):
    decision = gate.traffic_gate(health, error_rate, _PARAMS)

    assert not decision.allowed
    assert any(reason in r for r in decision.reasons)


def test_parse_bot_outcomes_reads_request_gauge_only():
    outcomes = gate.parse_bot_outcomes(_METRICS_BEFORE.decode())

    assert outcomes == {
        "pending": 2.0,
        "committed": 100.0,
        "failed": 1.0,
        "unknown": 0.0,
    }


def test_signals_measure_error_rate_over_window():
    bodies = iter([_METRICS_BEFORE, _METRICS_AFTER])
    fake = _FakeTime()
    with http_fixture.serve(
        {"/metrics": lambda: (200, next(bodies)), "/readyz": (200, b"{}")}
    ) as url:
        signals = gate.HttpTrafficSignals(
            health_url=url + "/readyz",
            bot_metrics_url=url + "/metrics",
            params=_PARAMS,
            clock=fake.clock,
            sleep=fake.sleep,
        )

        error_rate = signals.probe_error_rate()

    # 결과가 정해진 요청 93건 중 실패·불명 3건.
    assert error_rate == pytest.approx(3 / 93)
    assert fake.now == 30


def test_signals_error_rate_unmeasurable_without_new_requests():
    fake = _FakeTime()
    with http_fixture.serve({"/metrics": (200, _METRICS_BEFORE)}) as url:
        signals = gate.HttpTrafficSignals(
            health_url=url + "/readyz",
            bot_metrics_url=url + "/metrics",
            params=_PARAMS,
            clock=fake.clock,
            sleep=fake.sleep,
        )

        assert signals.probe_error_rate() is None


def test_signals_error_rate_unmeasurable_when_metrics_unreachable():
    signals = gate.HttpTrafficSignals(
        health_url="http://127.0.0.1:9/readyz",
        bot_metrics_url="http://127.0.0.1:9/metrics",
        params=_PARAMS,
        sleep=lambda _: None,
    )

    assert signals.probe_error_rate() is None


def test_signals_error_rate_unmeasurable_without_request_gauge():
    with http_fixture.serve({"/metrics": (200, b"up 1\n")}) as url:
        signals = gate.HttpTrafficSignals(
            health_url=url + "/readyz",
            bot_metrics_url=url + "/metrics",
            params=_PARAMS,
            sleep=lambda _: None,
        )

        assert signals.probe_error_rate() is None


def test_signals_count_consecutive_target_health():
    fake = _FakeTime()
    with http_fixture.serve({"/readyz": (200, b"{}")}) as url:
        signals = gate.HttpTrafficSignals(
            health_url=url + "/readyz",
            bot_metrics_url=url + "/metrics",
            params=_PARAMS,
            clock=fake.clock,
            sleep=fake.sleep,
        )

        assert signals.target_health_successes() == 3
