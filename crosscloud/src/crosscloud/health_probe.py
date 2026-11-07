"""앱 health 를 HTTP 로 점검한다 — 제어 어댑터와 트래픽 관문이 같이 쓴다.

health 주소는 DB 를 읽는 경로여야 한다(sample-app 은 /readyz. /healthz 는 DB
를 읽지 않는다). 성공은 HTTP 200 뿐이다.

  healthy = health_probe.is_healthy(url, timeout=5)
  streak = health_probe.consecutive_successes(
      lambda: health_probe.is_healthy(url, timeout=5), needed=3,
      deadline_seconds=25, interval=1)
"""

import collections.abc
import time
import urllib.error
import urllib.request


def is_healthy(url: str, timeout: float) -> bool:
    """주소에 GET 해서 200 이면 True. 연결 실패·다른 코드는 False."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def consecutive_successes(
    check: collections.abc.Callable[[], bool],
    *,
    needed: int,
    deadline_seconds: float,
    interval: float,
    clock: collections.abc.Callable[[], float] = time.monotonic,
    sleep: collections.abc.Callable[[float], None] = time.sleep,
) -> int:
    """연속 성공이 needed 에 닿거나 기한이 될 때까지 check 를 되풀이한다.

    Args:
        check: 한 번 점검해 성공 여부를 돌려주는 함수.
        needed: 필요한 연속 성공 수.
        deadline_seconds: 이 시간 안에서만 되풀이한다.
        interval: 점검 사이 대기(초).
        clock: 단조 시계.
        sleep: 대기 함수.

    Returns:
        끝났을 때의 연속 성공 수. needed 이상이면 통과다.
    """
    deadline = clock() + deadline_seconds
    streak = 0
    while streak < needed and clock() < deadline:
        streak = streak + 1 if check() else 0
        if streak < needed:
            sleep(interval)
    return streak
