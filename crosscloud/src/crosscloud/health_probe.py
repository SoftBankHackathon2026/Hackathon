"""앱 준비 상태(readiness)를 HTTP 로 점검한다.

점검 주소는 DB 를 읽는 경로여야 한다. sample-app 은 /readyz 다(/healthz 는 DB
를 읽지 않는다). 성공은 HTTP 200 이면서 JSON 본문의 status 가 "ok" 일 때뿐이다
(원가연 migration/ingress_client.py check_app_ready 와 같은 기준).

  ready = health_probe.is_healthy(app_url + "/readyz", timeout=5)
  streak = health_probe.consecutive_successes(
      lambda: health_probe.is_healthy(url, timeout=5), needed=3,
      deadline_seconds=25, interval=1)
"""

import collections.abc
import json
import time
import urllib.error
import urllib.request


def is_healthy(url: str, timeout: float) -> bool:
    """GET 이 200 이고 본문 status 가 ok 면 True. 그 밖은 모두 False."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status == 200 and _status_ok(body)
    except (urllib.error.URLError, OSError, ValueError):
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


def _status_ok(body: object) -> bool:
    return isinstance(body, dict) and body.get("status") == "ok"
