import http_fixture

from crosscloud import health_probe


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_is_healthy_only_on_http_200_with_status_ok():
    with http_fixture.serve(
        {
            "/readyz": (200, b'{"status":"ok"}'),
            "/odd": (200, b'{"status":"degraded"}'),
            "/text": (200, b"ok"),
            "/down": (503, b'{"status":"unavailable"}'),
        }
    ) as url:
        assert health_probe.is_healthy(url + "/readyz", timeout=2)
        assert not health_probe.is_healthy(url + "/odd", timeout=2)
        assert not health_probe.is_healthy(url + "/text", timeout=2)
        assert not health_probe.is_healthy(url + "/down", timeout=2)
        assert not health_probe.is_healthy(url + "/missing", timeout=2)


def test_is_healthy_false_when_unreachable():
    assert not health_probe.is_healthy("http://127.0.0.1:9/x", timeout=1)


def test_consecutive_successes_stops_once_enough():
    calls = []
    fake = _FakeTime()

    def check():
        calls.append(fake.now)
        return True

    streak = health_probe.consecutive_successes(
        check,
        needed=3,
        deadline_seconds=25,
        interval=1,
        clock=fake.clock,
        sleep=fake.sleep,
    )

    assert streak == 3
    assert len(calls) == 3


def test_consecutive_successes_resets_on_failure():
    results = iter([True, False, True, True, True])
    fake = _FakeTime()

    streak = health_probe.consecutive_successes(
        lambda: next(results),
        needed=3,
        deadline_seconds=25,
        interval=1,
        clock=fake.clock,
        sleep=fake.sleep,
    )

    assert streak == 3


def test_consecutive_successes_returns_streak_at_deadline():
    fake = _FakeTime()
    results = iter([True, True, False] + [True] * 100)

    streak = health_probe.consecutive_successes(
        lambda: next(results),
        needed=10,
        deadline_seconds=5,
        interval=1,
        clock=fake.clock,
        sleep=fake.sleep,
    )

    assert 0 < streak < 10
    assert fake.now <= 5
