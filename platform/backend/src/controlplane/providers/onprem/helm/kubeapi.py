from contextlib import contextmanager

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from controlplane.providers.contracts import ProviderUnavailable

READ_TIMEOUT = (3, 15)
TRANSIENT_STATUSES = frozenset({0, 429})


def transient(exc: ApiException) -> bool:
    status = exc.status or 0
    return status in TRANSIENT_STATUSES or status >= 500


@contextmanager
def observing():
    try:
        yield
    except ApiException as exc:
        if transient(exc):
            raise ProviderUnavailable("Kubernetes observation unavailable") from None
        raise
    except HTTPError:
        raise ProviderUnavailable("Kubernetes observation unavailable") from None


def read_optional(call, *args):
    with observing():
        try:
            return call(*args, _request_timeout=READ_TIMEOUT)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
