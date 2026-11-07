"""앱 준비 상태(readiness)를 HTTP 로 점검한다.

점검 주소는 DB 를 읽는 경로여야 한다. sample-app 은 /readyz 다(/healthz 는 DB
를 읽지 않는다). 성공은 HTTP 200 이면서 JSON 본문의 status 가 "ok" 일 때뿐이다
(원가연 migration/ingress_client.py check_app_ready 와 같은 기준).

  ready = health_probe.is_healthy(app_url + "/readyz", timeout=5)
  problems = health_probe.app_identity_problems(
      app_url, database="shop_b", environment="gcp", timeout=5)
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
    code, body = _get_json(url, timeout)
    return code == 200 and body.get("status") == "ok"


def app_identity_problems(
    base_url: str, *, database: str, environment: str | None, timeout: float
) -> tuple[str, ...]:
    """앱이 준비됐고 기대한 DB 에 붙었는지 본다. 문제 목록(없으면 빈 tuple).

    /readyz 준비와, /api/info 의 database.name·environment 가 기대와 같고
    writes_paused 가 아닌지를 본다(원가연 check_app_ready 와 같은 기준).

    Args:
        base_url: 앱 기준 주소.
        database: 기대하는 DB 이름(current_database()).
        environment: 기대하는 배포 환경(DEPLOY_ENV). None 이면 보지 않는다.
        timeout: 요청마다의 제한 시간(초).
    """
    base = base_url.rstrip("/")
    problems = []
    if not is_healthy(base + "/readyz", timeout):
        problems.append("/readyz 준비 안 됨")
    code, info = _get_json(base + "/api/info", timeout)
    if code != 200:
        problems.append(f"/api/info {code}")
        return tuple(problems)
    db = info.get("database")
    name = db.get("name") if isinstance(db, dict) else None
    if name != database:
        problems.append(f"database={name} (기대 {database})")
    if environment is not None and info.get("environment") != environment:
        problems.append(
            f"environment={info.get('environment')} (기대 {environment})"
        )
    if info.get("writes_paused") is True:
        problems.append("writes_paused=true")
    return tuple(problems)


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


def _get_json(url: str, timeout: float) -> tuple[int | None, dict]:
    """GET 결과 (코드, JSON 객체). 연결 실패면 None, 본문이 객체가 아니면 {}."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        code, raw = error.code, error.read()
    except (urllib.error.URLError, OSError):
        return None, {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        return code, {}
    return code, body if isinstance(body, dict) else {}
