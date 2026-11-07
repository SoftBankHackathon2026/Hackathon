"""원가연 durable-ingress(접수 큐) 제어 API 클라이언트.

계약 정본은 sample-app docs/durable-ingress.md·src/durable-ingress/service.js
이고, 호출 방식은 원가연 migration/ingress_client.py 를 따른다. 제어 API 는
epoch 로 보호되므로 /target·/resume 은 직전에 읽은 epoch 를 함께 보낸다.

응답이 모호하면(필드 누락·모르는 값·settled 아님) 성공으로 보지 않고
IngressError 를 낸다. 오류 메시지에는 응답의 오류 코드만 담고 토큰은 담지
않는다.

  ingress = ingress_control.IngressControl(url, token=os.environ.get(...))
  state = ingress.pause()
  state = ingress.set_target("gcp", state.epoch)
"""

import dataclasses
import json
import re
import urllib.error
import urllib.request
from typing import Any, Literal

_EPOCH = re.compile(r"\d+")
_DEFAULT_TIMEOUT_SECONDS = 20.0


class IngressError(RuntimeError):
    """제어 API 호출이 실패했거나 응답이 모호하다.

    Attributes:
        status: HTTP 상태 코드. 연결 실패·형식 오류면 None.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        """메시지와 HTTP 상태 코드를 담는다."""
        super().__init__(message)
        self.status = status


@dataclasses.dataclass(frozen=True)
class IngressState:
    """제어 API /status 의 핵심 필드.

    Attributes:
        mode: running 이면 대상 앱으로 전달 중, paused 면 접수만 한다.
        target: INGRESS_TARGETS 의 대상 이름.
        epoch: 대상 변경 세대. /target 마다 1 씩 는다.
        in_flight: 전달 중인 요청 수(0 또는 1).
    """

    mode: Literal["running", "paused"]
    target: str
    epoch: str
    in_flight: int


class IngressControl:
    """한 durable-ingress 인스턴스의 제어 API."""

    def __init__(
        self,
        control_url: str,
        *,
        token: str | None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """제어 주소와 Bearer 토큰(없으면 None)을 받는다."""
        self._base = control_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def status(self) -> IngressState:
        """GET /status."""
        return _parse_state(self._call("GET", "/status", None))

    def pause(self) -> IngressState:
        """POST /pause. 진행 중 전달이 끝나 settled:true 일 때만 성공이다."""
        body = self._call("POST", "/pause", {})
        if body.get("settled") is not True:
            raise IngressError("pause 응답이 settled:true 가 아니다")
        return _parse_state(body)

    def set_target(self, target: str, expected_epoch: str) -> IngressState:
        """POST /target. paused·in_flight 0·epoch 일치일 때만 바뀐다."""
        return _parse_state(
            self._call(
                "POST",
                "/target",
                {"target": target, "expected_epoch": expected_epoch},
            )
        )

    def resume(self, expected_epoch: str) -> IngressState:
        """POST /resume. epoch 가 최신일 때만 전달을 재개한다."""
        return _parse_state(
            self._call("POST", "/resume", {"expected_epoch": expected_epoch})
        )

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self._base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout
            ) as response:
                return _json_object(response.read())
        except urllib.error.HTTPError as error:
            code = _error_code(error.read())
            raise IngressError(
                f"{method} {path} → HTTP {error.code} ({code})", error.code
            ) from None
        except (urllib.error.URLError, OSError) as error:
            raise IngressError(
                f"{method} {path} 연결 실패: {type(error).__name__}"
            ) from None


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise IngressError("응답이 JSON 이 아니다(모호)") from None
    if not isinstance(body, dict):
        raise IngressError("응답이 JSON 객체가 아니다(모호)")
    return body


def _error_code(raw: bytes) -> str:
    try:
        error = json.loads(raw.decode("utf-8")).get("error")
    except (ValueError, AttributeError):
        return "unknown"
    if isinstance(error, dict):
        error = error.get("code")
    return error if isinstance(error, str) else "unknown"


def _parse_state(body: dict[str, Any]) -> IngressState:
    mode, target = body.get("mode"), body.get("target")
    epoch, in_flight = body.get("epoch"), body.get("in_flight")
    valid = (
        mode in ("running", "paused")
        and isinstance(target, str)
        and bool(target)
        and isinstance(epoch, str)
        and bool(_EPOCH.fullmatch(epoch))
        and type(in_flight) is int
        and in_flight >= 0
    )
    if not valid:
        raise IngressError("ingress 상태 응답이 모호하다")
    return IngressState(mode, target, epoch, in_flight)
