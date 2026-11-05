import threading
import time

from controlplane.providers.onprem.connection import KubernetesClients, connect

CONNECTION_TTL = 300
_connections: dict = {}
_connections_lock = threading.Lock()


def _key(settings) -> tuple:
    return (str(settings.kubeconfig), settings.context, settings.server)


def shared_clients(settings) -> KubernetesClients:
    with _connections_lock:
        cached = _connections.get(_key(settings))
        if cached is None or time.monotonic() - cached[1] > CONNECTION_TTL:
            cached = _connections[_key(settings)] = (
                connect(
                    kubeconfig=settings.kubeconfig,
                    context=settings.context,
                    expected_server=settings.server,
                ),
                time.monotonic(),
            )
    return cached[0]


def forget_clients(settings) -> None:
    with _connections_lock:
        _connections.pop(_key(settings), None)
