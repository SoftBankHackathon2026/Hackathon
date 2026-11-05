"""마이그레이션 중 스모크 테스트. sample-app 요청 API 로 생성·재시도·조회·복제를 보고 OPA 의 target_app_db 검사를 만든다.

  python -m migration.smoke run --stage pre_cutover --job-id forward_001 \
      --source-url https://source-app --target-url https://target-app
  python -m migration.smoke run --stage final_verify --job-id forward_001 \
      --source-url https://source-app --target-url https://target-app --request-id <pre_cutover 의 detail.request_id>

단계별 규칙 (대상 DB 에 원본에 없는 행을 만들지 않는 것이 핵심이다)
  pre_cutover   원본 앱 POST·재시도·GET → 대상 앱 GET. 대상 앱에는 POST 하지 않는다.
                대상 행은 CDC 로만 생긴다. 대상에 직접 쓰면 verify(pre) 의 T-B 와 DRAINING 의 건수 일치가 깨진다.
  final_verify  양쪽 다 GET 만. pre_cutover 가 만든 행을 원본과 대상에서 읽어 비교한다.
                이때는 원본 INSERT 가 REVOKE 됐고 커넥터도 지워져, 새 행은 쓸 수도 복제될 수도 없다.

스모크 행은 run_id=smoke_<job_id>, phase=smoke 로 구분하고 지우지 않는다 (앱·sink 모두 DELETE 권한이 없다).
READY 이후에 실행한다. 더 이르면 CATCHING_UP 의 live 행 수에 스모크 행이 함께 세진다.

판정 (policies/migration.rego APP-01)
  status=pass   관측 성공. 사실은 detail 의 boolean 으로 전한다. 확정 실패도 pass + false 다
                (APP-01-ORDER-CHECK-FAILED·TARGET-DB-UNREACHABLE 은 pass 일 때만 평가된다).
  status=unknown 판단 불가. 인증 거절, 쓰기 정지, 복제 대기 시간 초과 등.
  status=error  관측 실패. 연결 불가, HTTP 타임아웃, 앱이 아닌 응답(프록시 오류 등).

토큰은 환경변수로만 받고 로그·결과·예외에 남기지 않는다. 종료 코드: 0 통과, 1 설정 오류, 2 확정 실패, 3 unknown/error.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from .policy_client import build_check

log = logging.getLogger("migration.smoke")

CHECK_NAME = "target_app_db"
CHECK_SOURCE = "migration.smoke"
SMOKE_PHASE = "smoke"
STAGES: tuple[str, ...] = ("pre_cutover", "final_verify")
STEPS: tuple[str, ...] = ("create", "retry", "read_back", "replicated")
RECORD_FIELDS: frozenset[str] = frozenset({"request_id", "run_id", "phase", "request_no", "payload", "created_at"})

OK, FAILED, UNKNOWN, ERROR, SKIPPED, NOT_RUN = "ok", "failed", "unknown", "error", "skipped", "not_run"

DEFAULT_TOKEN_ENV = "SMOKE_API_TOKEN"
MAX_BODY_BYTES = 1024 * 1024
MAX_RUN_ID_LENGTH = 200  # sample-app docs/api.md 입력 한도
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
# urlsplit 의 hostname 에 전체 일치시킨다. 라벨 문자를 제한해 `evil.com\.svc` 같은 이름도 통과하지 못한다.
_CLUSTER_HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+svc(?:\.cluster\.local)?")
_TOKEN_RE = re.compile(r"[A-Za-z0-9\-._~+/]{32,}=*")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_REQUEST_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

EXIT_PASS, EXIT_CONFIG, EXIT_FAILED, EXIT_UNVERIFIED = 0, 1, 2, 3


class SmokeConfigError(ValueError):
    """호출자 설정 오류. 요청을 하나도 보내기 전에 낸다. 메시지에 토큰·URL 인증정보를 넣지 않는다."""


@dataclass(frozen=True)
class Response:
    """HTTP 결과 하나. code 가 None 이면 응답을 받지 못했고 failure 에 예외 종류만 남는다 (예외 문구는 남기지 않는다)."""

    code: int | None
    body: dict[str, Any] | None = None
    failure: str | None = None

    @property
    def error_code(self) -> str | None:
        """계약의 error.code. 응답 본문을 결과에 옮기는 유일한 값이라 형식을 제한한다."""
        err = (self.body or {}).get("error")
        code = err.get("code") if isinstance(err, dict) else None
        return code if isinstance(code, str) and _ERROR_CODE_RE.fullmatch(code) else None

    @property
    def record(self) -> Any:
        return (self.body or {}).get("request")

    @property
    def environment(self) -> str | None:
        env = (self.body or {}).get("environment")
        return env if isinstance(env, str) and env else None


@dataclass
class Step:
    result: str = NOT_RUN
    http_code: int | None = None
    reason: str | None = None
    attempts: int | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"result": self.result, "http_code": self.http_code}
        if self.reason:
            out["reason"] = self.reason
        if self.attempts is not None:
            out["attempts"] = self.attempts
        return out


@dataclass
class SmokeResult:
    """스모크 한 번의 관측 결과. to_check() 로 OPA 입력 형식이 된다."""

    stage: str
    job_id: str
    run_id: str
    request_id: str | None
    steps: dict[str, Step] = field(default_factory=lambda: {name: Step() for name in STEPS})
    target_probe: Step | None = None
    db_connected: bool = False
    source_environment: str | None = None
    target_environment: str | None = None

    def _results(self) -> list[str]:
        probe = [self.target_probe.result] if self.target_probe else []
        return probe + [step.result for step in self.steps.values()]

    @property
    def order_query_ok(self) -> bool:
        """스텝이 전부 통과했을 때만 true. final_verify 의 create/retry 는 하지 않는 스텝이라 skipped 를 허용한다."""
        results = {name: step.result for name, step in self.steps.items()}
        if self.stage == "final_verify":
            return results == {"create": SKIPPED, "retry": SKIPPED, "read_back": OK, "replicated": OK}
        return all(r == OK for r in results.values())

    @property
    def status(self) -> str:
        results = self._results()
        if ERROR in results:
            return ERROR
        if UNKNOWN in results:
            return UNKNOWN
        if FAILED in results or self.order_query_ok:
            return "pass"
        return UNKNOWN  # 실패도 통과도 아닌 상태는 통과로 만들지 않는다

    @property
    def exit_code(self) -> int:
        if self.status != "pass":
            return EXIT_UNVERIFIED
        return EXIT_PASS if self.db_connected and self.order_query_ok else EXIT_FAILED

    def to_check(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "db_connected": self.db_connected,
            "order_query_ok": self.order_query_ok,
            "steps": {name: self.steps[name].as_dict() for name in STEPS},
            "stage": self.stage,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "source_environment": self.source_environment,
            "target_environment": self.target_environment,
        }
        if self.target_probe is not None:
            detail["target_probe"] = self.target_probe.as_dict()
        return build_check(self.status, detail, source=CHECK_SOURCE)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가지 않는다. 따라가면 Authorization 헤더가 다른 호스트로 넘어갈 수 있다."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class RequestApi:
    """sample-app 요청 API 클라이언트. HTTP 오류와 연결 실패를 예외 없이 Response 로 돌려준다."""

    def __init__(self, base_url: str, token: str | None, timeout: float) -> None:
        self.base_url = _validated_base_url(base_url, token)
        self._token = token
        self.timeout = timeout
        # 환경의 HTTP(S)_PROXY 로 토큰이 새지 않게 프록시를 끈다.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def __repr__(self) -> str:
        return f"RequestApi({self.base_url!r})"

    def post(self, record: dict[str, Any]) -> Response:
        return self._call("POST", "/api/requests", record)

    def get(self, request_id: str) -> Response:
        return self._call("GET", f"/api/requests/{request_id}", None)

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> Response:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(resp.status, _parse_body(resp.read(MAX_BODY_BYTES)))
        except urllib.error.HTTPError as exc:
            try:
                return Response(exc.code, _parse_body(exc.read(MAX_BODY_BYTES)))
            except OSError as read_exc:
                return Response(None, failure=type(read_exc).__name__)
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            return Response(None, failure=type(exc.reason).__name__)
        except Exception as exc:  # noqa: BLE001 - 어떤 전송 실패든 관측 실패다. 문구에 요청 정보가 섞일 수 있어 종류만 남긴다
            return Response(None, failure=type(exc).__name__)


def _parse_body(raw: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validated_base_url(url: str, token: str | None) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SmokeConfigError("앱 URL 은 http(s)://호스트 형식이어야 한다")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise SmokeConfigError("앱 URL 에 인증정보·쿼리·fragment 를 넣지 않는다")
    if token and parts.scheme != "https" and not _allows_cleartext_token(parts.hostname):
        raise SmokeConfigError("토큰을 http 로 보낼 수 있는 곳은 루프백과 *.svc, *.svc.cluster.local 뿐이다. 그 밖에는 https 를 쓴다")
    return url.rstrip("/")


def _allows_cleartext_token(hostname: str) -> bool:
    """에이전트 파드가 클러스터 내부 Service 를 http 로 부르는 경로를 허용한다. traffic-bot 규칙보다 넓은 예외다."""
    return hostname in _LOOPBACK_HOSTS or _CLUSTER_HOST_RE.fullmatch(hostname) is not None


def token_from_env(name: str = DEFAULT_TOKEN_ENV, env: dict[str, str] | None = None) -> str | None:
    """환경변수에서 토큰을 읽는다. 키가 없으면 인증 없이 보낸다. 값이 있는데 형식이 틀리면 값 없이 거절한다."""
    environ = os.environ if env is None else env
    if name not in environ:
        return None
    token = environ[name]
    if not _TOKEN_RE.fullmatch(token):
        raise SmokeConfigError(f"환경변수 {name} 의 토큰 형식이 틀렸다 (32자 이상 RFC 6750 b64token)")
    return token


def smoke_run_id(job_id: str) -> str:
    return f"smoke_{job_id}"


def new_record(job_id: str, *, now: datetime | None = None) -> dict[str, str]:
    """새 스모크 행. phase 는 seed·live 와 겹치지 않아 seed 검증과 phase 별 건수를 건드리지 않는다."""
    at = (now or datetime.now(UTC)).astimezone(UTC)
    return {
        "request_id": str(uuid.uuid4()),
        "run_id": smoke_run_id(job_id),
        "phase": SMOKE_PHASE,
        "request_no": str(int(at.timestamp() * 1000)),
        "payload": json.dumps({"synthetic": "migration smoke", "job_id": job_id}, separators=(",", ":")),
        "created_at": at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def _classify_failure(resp: Response, *, target_side: bool) -> tuple[str, str]:
    """기대와 다른 응답을 (결과, 이유) 로 나눈다. 앱이 계약대로 말한 거절만 확정 실패다."""
    if resp.code is None:
        return ERROR, f"transport:{resp.failure}"
    code = resp.error_code
    if resp.code >= 300 and code is None or resp.body is None:
        return ERROR, "non_contract_response"
    if resp.code in (401, 403):
        return UNKNOWN, f"auth:{code}"
    if resp.code == 503:
        if code == "DB_UNAVAILABLE" and target_side:
            return FAILED, "target_db_unavailable"
        return UNKNOWN, f"unavailable:{code}"
    return FAILED, f"unexpected_response:{code}" if code else "unexpected_response"


def _is_not_found(resp: Response) -> bool:
    """앱이 DB 를 조회한 뒤 준 404 만 인정한다. ingress 의 404 는 조회 증거가 아니다."""
    return resp.code == 404 and resp.error_code == "REQUEST_NOT_FOUND"


def _db_answered(resp: Response) -> bool:
    return (resp.code == 200 and resp.record is not None) or _is_not_found(resp)


def _set_failure(step: Step, resp: Response, *, target_side: bool = False) -> None:
    step.result, step.reason = _classify_failure(resp, target_side=target_side)
    step.http_code = resp.code


def _check_write(step: Step, resp: Response, record: dict[str, str], *, expect_code: int, expect_created: bool) -> None:
    step.http_code = resp.code
    if resp.code != expect_code:
        _set_failure(step, resp)
        if resp.code in (200, 201) and resp.body is not None:  # 201/200 이 뒤바뀐 응답은 앱이 계약을 어긴 것이다
            step.result, step.reason = FAILED, "unexpected_status"
    elif resp.body is None:
        _set_failure(step, resp)
    elif resp.body.get("created") is not expect_created:
        step.result, step.reason = FAILED, "created_flag_mismatch"
    elif resp.record != record:
        step.result, step.reason = FAILED, "record_mismatch"
    else:
        step.result = OK


class _Runner:
    def __init__(
        self,
        source: RequestApi,
        target: RequestApi,
        *,
        replication_timeout: float,
        poll_interval: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> None:
        self.source, self.target = source, target
        self.replication_timeout = replication_timeout
        self.poll_interval = poll_interval
        self.sleep, self.clock = sleep, clock

    def pre_cutover(self, job_id: str, *, now: datetime | None) -> SmokeResult:
        record = new_record(job_id, now=now)
        rid = record["request_id"]
        result = SmokeResult("pre_cutover", job_id, record["run_id"], rid)

        # 대상을 먼저 본다. 대상 앱을 확인할 수 없으면 원본에 행을 만들 이유가 없다.
        probe_resp = self.target.get(rid)
        result.target_probe = probe = Step(http_code=probe_resp.code)
        result.db_connected = _db_answered(probe_resp)
        if _is_not_found(probe_resp):
            probe.result = OK
        else:
            _set_failure(probe, probe_resp, target_side=True)
            if probe_resp.code == 200:  # 방금 만든 UUID 가 이미 있다. 무엇을 보고 있는지 믿을 수 없다
                probe.result, probe.reason = UNKNOWN, "id_already_on_target"
            return result

        create = result.steps["create"]
        resp = self.source.post(record)
        _check_write(create, resp, record, expect_code=201, expect_created=True)
        result.source_environment = resp.environment
        if create.result != OK:
            # 인증 거절·WRITES_PAUSED 는 DB 쓰기 전에 나온다. 그 밖의 무응답·장애는 행이 남았을 수 있다.
            if create.result in (ERROR, UNKNOWN) and resp.error_code in (None, "DB_UNAVAILABLE"):
                create.reason = f"{create.reason};commit_uncertain"
            return result

        _check_write(result.steps["retry"], self.source.post(record), record, expect_code=200, expect_created=False)

        read_back = result.steps["read_back"]
        resp = self.source.get(rid)
        read_back.http_code = resp.code
        if resp.code == 200 and resp.record == record:
            read_back.result = OK
        elif resp.code == 200 and resp.body is not None:
            read_back.result, read_back.reason = FAILED, "record_mismatch"
        else:
            _set_failure(read_back, resp)

        self._poll_replicated(result, record)
        return result

    def _poll_replicated(self, result: SmokeResult, record: dict[str, str]) -> None:
        step = result.steps["replicated"]
        deadline = self.clock() + self.replication_timeout
        step.attempts = 0
        while True:
            resp = self.target.get(record["request_id"])
            step.attempts += 1
            step.http_code = resp.code
            result.db_connected = _db_answered(resp)
            if resp.code == 200 and resp.body is not None:
                self._judge_target_record(result, resp, record)
                return
            if self.clock() >= deadline:
                break
            self.sleep(min(self.poll_interval, max(deadline - self.clock(), 0)))
        if _is_not_found(resp):
            # 앱·DB 는 응답했다. 복제 지연인지 복제 고장인지 여기서 가를 수 없어 재수집 대상(hold)으로 둔다.
            step.result, step.reason = UNKNOWN, "replication_not_seen_within_timeout"
        else:
            _set_failure(step, resp, target_side=True)

    @staticmethod
    def _judge_target_record(result: SmokeResult, resp: Response, record: Any) -> None:
        step = result.steps["replicated"]
        result.target_environment = resp.environment
        if resp.record != record:
            step.result, step.reason = FAILED, "record_mismatch"
        elif resp.environment is None or result.source_environment is None:
            step.result, step.reason = UNKNOWN, "environment_missing"
        elif resp.environment == result.source_environment:
            # 같은 환경을 두 번 읽으면 복제가 없어도 보인다. 복제 증거가 될 수 없다.
            step.result, step.reason = UNKNOWN, "source_and_target_same_environment"
        else:
            step.result = OK

    def final_verify(self, job_id: str, request_id: str | None) -> SmokeResult:
        """GET 만 한다. 기준 행은 pre_cutover 의 detail.request_id 로 받는다."""
        rid = request_id.lower() if request_id else None
        valid = rid is not None and _REQUEST_ID_RE.fullmatch(rid) is not None
        result = SmokeResult("final_verify", job_id, smoke_run_id(job_id), rid if valid else None)
        result.steps["create"].result = SKIPPED
        result.steps["retry"].result = SKIPPED
        read_back, replicated = result.steps["read_back"], result.steps["replicated"]
        if not valid:
            read_back.result, read_back.reason = UNKNOWN, "request_id_missing_or_invalid"
            # 대상 앱 연결은 기준 행 없이도 볼 수 있다. 비교만 못 한다.
            result.db_connected = _db_answered(self.target.get(str(uuid.uuid4())))
            return result

        source_resp = self.source.get(rid)
        read_back.http_code = source_resp.code
        result.source_environment = source_resp.environment
        source_record: Any = None
        if source_resp.code == 200 and source_resp.body is not None:
            source_record = source_resp.record
            if not isinstance(source_record, dict) or set(source_record) != RECORD_FIELDS:
                read_back.result, read_back.reason = FAILED, "record_mismatch"
                source_record = None
            elif source_record.get("run_id") != result.run_id:
                read_back.result, read_back.reason = UNKNOWN, "not_a_smoke_row_of_this_job"
                source_record = None
            else:
                read_back.result = OK
        elif _is_not_found(source_resp):
            read_back.result, read_back.reason = UNKNOWN, "reference_row_not_on_source"
        else:
            _set_failure(read_back, source_resp)

        target_resp = self.target.get(rid)
        result.db_connected = _db_answered(target_resp)
        result.target_environment = target_resp.environment
        replicated.http_code = target_resp.code
        if source_record is None:
            if not result.db_connected:
                _set_failure(replicated, target_resp, target_side=True)
            return result
        if target_resp.code == 200 and target_resp.body is not None:
            self._judge_target_record(result, target_resp, source_record)
        elif _is_not_found(target_resp):
            # 이 단계는 DRAINING·verify 를 지난 뒤다. 원본에 있는 행이 대상에 없으면 대기로 풀리지 않는다.
            replicated.result, replicated.reason = FAILED, "source_row_missing_on_target"
        else:
            _set_failure(replicated, target_resp, target_side=True)
        return result


def run_smoke(
    stage: str,
    *,
    job_id: str,
    source_url: str,
    target_url: str,
    token: str | None = None,
    request_id: str | None = None,
    http_timeout: float = 5.0,
    replication_timeout: float = 30.0,
    poll_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: datetime | None = None,
) -> SmokeResult:
    """스모크를 실행한다. 관측 실패는 예외가 아니라 결과의 status 로 나온다. 예외는 설정 오류뿐이다."""
    if stage not in STAGES:
        raise SmokeConfigError(f"stage 는 {STAGES} 중 하나다: {stage!r}")
    if not job_id or len(smoke_run_id(job_id)) > MAX_RUN_ID_LENGTH:
        raise SmokeConfigError(f"job_id 는 비어 있지 않고 run_id 가 {MAX_RUN_ID_LENGTH}자 이하여야 한다")
    if http_timeout <= 0 or replication_timeout < 0 or poll_interval < 0:
        raise SmokeConfigError("시간 설정이 잘못됐다")
    runner = _Runner(
        RequestApi(source_url, token, http_timeout),
        RequestApi(target_url, token, http_timeout),
        replication_timeout=replication_timeout,
        poll_interval=poll_interval,
        sleep=sleep,
        clock=clock,
    )
    if stage == "final_verify":
        result = runner.final_verify(job_id, request_id)
    else:
        result = runner.pre_cutover(job_id, now=now)
    log.info("스모크 %s: status=%s db_connected=%s order_query_ok=%s", stage, result.status,
             result.db_connected, result.order_query_ok)
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m migration.smoke", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    subs = p.add_subparsers(dest="command", required=True)
    r = subs.add_parser("run", help="스모크를 실행하고 target_app_db check JSON 을 출력한다")
    r.add_argument("--stage", required=True, choices=STAGES)
    r.add_argument("--job-id", required=True, help="스모크 행의 run_id(smoke_<job_id>)가 된다")
    r.add_argument("--source-url", required=True)
    r.add_argument("--target-url", required=True)
    r.add_argument("--request-id", help="final_verify 에서 비교할 pre_cutover 행 ID (그 결과의 detail.request_id)")
    r.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="토큰을 담은 환경변수 이름. 토큰 값을 인자로 받지 않는다")
    r.add_argument("--http-timeout", type=float, default=5.0)
    r.add_argument("--replication-timeout", type=float, default=30.0)
    r.add_argument("--poll-interval", type=float, default=1.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        result = run_smoke(
            args.stage,
            job_id=args.job_id,
            source_url=args.source_url,
            target_url=args.target_url,
            token=token_from_env(args.token_env),
            request_id=args.request_id,
            http_timeout=args.http_timeout,
            replication_timeout=args.replication_timeout,
            poll_interval=args.poll_interval,
        )
    except SmokeConfigError as exc:
        log.error("설정 오류: %s", exc)
        return EXIT_CONFIG
    print(json.dumps(result.to_check(), ensure_ascii=False, indent=2))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
