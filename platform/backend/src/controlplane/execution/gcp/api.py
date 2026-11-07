from dataclasses import dataclass
from typing import Any

from requests import ConnectTimeout, RequestException
from urllib3.exceptions import NewConnectionError

from controlplane.providers.contracts import ProviderUnavailable

JsonObject = dict[str, Any]


class RequestNotDelivered(ProviderUnavailable):
    pass


def _never_connected(exc: RequestException) -> bool:
    cause = exc.args[0] if exc.args else None
    return isinstance(exc, ConnectTimeout) or isinstance(
        getattr(cause, "reason", None), NewConnectionError
    )


@dataclass(frozen=True)
class ApiProfile:
    label: str
    rejected: str
    failure: type[ValueError]
    timeout: int = 30
    invalid: str | None = None


class GcpApiClient:
    def __init__(self, session, profile: ApiProfile):
        self.session = session
        self.profile = profile

    def call(self, method: str, url: str, *, body: JsonObject | None = None) -> JsonObject:
        return self._parse(self._fetch(method, url, body))

    def call_unless_missing(
        self, method: str, url: str, *, body: JsonObject | None = None
    ) -> JsonObject | None:
        response = self._fetch(method, url, body)
        return None if response.status_code == 404 else self._parse(response)

    def call_unless_conflict(
        self, method: str, url: str, *, body: JsonObject | None = None
    ) -> JsonObject | None:
        response = self._fetch(method, url, body)
        return None if response.status_code == 409 else self._parse(response)

    def _fetch(self, method: str, url: str, body: JsonObject | None):
        try:
            return self.session.request(method, url, json=body, timeout=self.profile.timeout)
        except RequestException as exc:
            if _never_connected(exc):
                raise RequestNotDelivered(f"{self.profile.label} connection failure") from None
            raise ProviderUnavailable(f"{self.profile.label} transport failure") from None

    def _parse(self, response) -> JsonObject:
        profile = self.profile
        if response.status_code == 429:
            raise RequestNotDelivered(f"{profile.label} rate limited")
        if response.status_code >= 500:
            raise ProviderUnavailable(f"{profile.label} temporarily unavailable")
        if not 200 <= response.status_code < 300:
            raise profile.failure(profile.rejected.format(status=response.status_code))
        try:
            return response.json()
        except ValueError:
            raise ProviderUnavailable(
                profile.invalid or f"Invalid {profile.label} response"
            ) from None


class ApiBacked:
    api: GcpApiClient

    @property
    def session(self):
        return self.api.session

    @session.setter
    def session(self, value) -> None:
        self.api.session = value
