import hashlib
import hmac
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from controlplane.core.secrets import secret_from_environment

COOKIE = "oneport_pin"
COOKIE_PATH = "/api"
TTL_SECONDS = 12 * 60 * 60
MAX_FAILURES = 10
FAILURE_WINDOW_SECONDS = 10 * 60
PIN_PATTERN = re.compile(r"^\d{4}$")
OPEN_PATHS = re.compile(
    r"^/api/v1/(access-pin$|ingest/|enrollments/scripts/|enrollments/[^/]+/complete$)"
)


@dataclass
class PinGate:
    pin: str
    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    failures: deque[float] = field(default_factory=deque)

    def _signature(self, expires: int) -> str:
        return hmac.new(self.key, str(expires).encode(), hashlib.sha256).hexdigest()

    def issue(self, now: float) -> str:
        expires = int(now) + TTL_SECONDS
        return f"{expires}.{self._signature(expires)}"

    def valid(self, cookie: str | None, now: float) -> bool:
        expires, _, signature = (cookie or "").partition(".")
        if not expires.isdigit() or int(expires) < now:
            return False
        return hmac.compare_digest(signature, self._signature(int(expires)))

    def locked(self, now: float) -> bool:
        while self.failures and self.failures[0] <= now - FAILURE_WINDOW_SECONDS:
            self.failures.popleft()
        return len(self.failures) >= MAX_FAILURES

    def attempt(self, pin: str, now: float) -> bool:
        if self.locked(now):
            return False
        if hmac.compare_digest(pin.encode(), self.pin.encode()):
            return True
        self.failures.append(now)
        return False


def gate_from_environment() -> PinGate | None:
    pin = secret_from_environment("ONEPORT_ACCESS_PIN").get_secret_value().strip()
    if not pin:
        return None
    if not PIN_PATTERN.fullmatch(pin):
        raise ValueError("ONEPORT_ACCESS_PIN must be exactly 4 digits")
    return PinGate(pin)


class PinMiddleware:
    def __init__(self, app: ASGIApp, gate: PinGate) -> None:
        self.app = app
        self.gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or OPEN_PATHS.match(path):
            await self.app(scope, receive, send)
            return
        if self.gate.valid(Request(scope).cookies.get(COOKIE), time.time()):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"detail": "접속 비밀번호를 먼저 입력하세요.", "pin_required": True}, status_code=401
        )
        await response(scope, receive, send)


class PinStatus(BaseModel):
    required: bool
    unlocked: bool


class PinAttempt(BaseModel):
    pin: str


router = APIRouter(prefix="/api/v1", tags=["access-pin"])


def _gate(request: Request) -> PinGate | None:
    return getattr(request.app.state, "access_pin", None)


@router.get("/access-pin")
def pin_status(request: Request) -> PinStatus:
    gate = _gate(request)
    if gate is None:
        return PinStatus(required=False, unlocked=True)
    return PinStatus(required=True, unlocked=gate.valid(request.cookies.get(COOKIE), time.time()))


@router.post("/access-pin", response_model=PinStatus)
def unlock(attempt: PinAttempt, request: Request, response: Response) -> PinStatus | JSONResponse:
    gate = _gate(request)
    if gate is None:
        return PinStatus(required=False, unlocked=True)
    now = time.time()
    if gate.locked(now):
        return JSONResponse(
            {"detail": "여러 번 틀려서 잠시 잠갔어요. 10분 뒤에 다시 시도하세요."}, status_code=429
        )
    if not gate.attempt(attempt.pin, now):
        return JSONResponse({"detail": "비밀번호가 맞지 않아요."}, status_code=401)
    response.set_cookie(
        COOKIE,
        gate.issue(now),
        max_age=TTL_SECONDS,
        path=COOKIE_PATH,
        httponly=True,
        samesite="strict",
        secure=True,
    )
    return PinStatus(required=True, unlocked=True)


def install(app: FastAPI, gate: PinGate | None) -> None:
    app.state.access_pin = gate
    app.include_router(router)
    if gate is not None:
        app.add_middleware(PinMiddleware, gate=gate)
