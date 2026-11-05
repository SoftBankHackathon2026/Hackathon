import time
from collections.abc import Callable

from controlplane.core.errors import TooManyRequests, Unauthenticated
from controlplane.repositories.accounts import AccountStore
from controlplane.schemas.accounts import Credentials, Identity, SignUp

FAILURE_WINDOW_SECONDS = 300
MAX_FAILURES = 5
MAX_TRACKED_EMAILS = 10_000


class LoginThrottle:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._failures: dict[str, list[float]] = {}
        self._clock = clock

    def _recent(self, email: str) -> list[float]:
        now = self._clock()
        return [at for at in self._failures.get(email, []) if now - at < FAILURE_WINDOW_SECONDS]

    def check(self, email: str) -> None:
        if len(self._recent(email)) >= MAX_FAILURES:
            raise TooManyRequests("로그인 시도가 많습니다. 5분 뒤 다시 시도하세요.")

    def record_failure(self, email: str) -> None:
        if len(self._failures) > MAX_TRACKED_EMAILS:
            self._failures.clear()
        self._failures[email] = [*self._recent(email), self._clock()]

    def reset(self, email: str) -> None:
        self._failures.pop(email, None)


class AuthService:
    def __init__(self, accounts: AccountStore, throttle: LoginThrottle) -> None:
        self.accounts = accounts
        self._throttle = throttle

    def sign_up(self, request: SignUp) -> tuple[Identity, str]:
        identity = self.accounts.sign_up(request)
        return identity, self.accounts.open_session(identity)

    def log_in(self, request: Credentials) -> tuple[Identity, str]:
        self._throttle.check(request.email)
        identity = self.accounts.authenticate(request)
        if identity is None:
            self._throttle.record_failure(request.email)
            raise Unauthenticated("이메일 또는 비밀번호가 맞지 않습니다.")
        self._throttle.reset(request.email)
        return identity, self.accounts.open_session(identity)

    def log_out(self, token: str | None) -> None:
        self.accounts.close_session(token)

    def identity(self, token: str | None) -> Identity | None:
        return self.accounts.identity(token)
