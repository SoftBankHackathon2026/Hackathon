from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from controlplane.providers.contracts import ProviderUnavailable

READ_TIMEOUT = (3, 15)


def read_optional(call, *args):
    try:
        return call(*args, _request_timeout=READ_TIMEOUT)
    except ApiException as exc:
        if exc.status == 404:
            return None
        if exc.status >= 500:
            raise ProviderUnavailable("Kubernetes observation unavailable") from None
        raise
    except HTTPError:
        raise ProviderUnavailable("Kubernetes observation unavailable") from None
