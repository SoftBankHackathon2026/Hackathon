"""Explicit TLS-verified, operator-owned kubeconfig; no implicit local/default context."""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from kubernetes import client, config


@dataclass
class KubernetesClients:
    api: client.ApiClient
    core: client.CoreV1Api
    apps: client.AppsV1Api
    batch: client.BatchV1Api
    networking: client.NetworkingV1Api
    authorization: client.AuthorizationV1Api
    custom: client.CustomObjectsApi

    def close(self):
        self.api.close()


def is_credential_free_https(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
    )


def connect(*, kubeconfig: Path, context: str, expected_server: str) -> KubernetesClients:
    """The kubeconfig may contain exec auth. Only use a trusted operator-owned file."""
    if not is_credential_free_https(expected_server):
        raise ValueError("A credential-free HTTPS API endpoint is required")
    if not context or not kubeconfig.is_file():
        raise ValueError("Explicit kubeconfig file and context are required")
    configuration = client.Configuration()
    config.load_kube_config(
        config_file=str(kubeconfig),
        context=context,
        client_configuration=configuration,
        persist_config=False,
    )
    if (
        configuration.host.rstrip("/") != expected_server.rstrip("/")
        or not configuration.verify_ssl
    ):
        raise ValueError("Kubeconfig endpoint/TLS does not match the registered cluster")
    configuration.retries = 0  # A timed-out POST must be reconciled, never blindly retried.
    configuration.debug = False
    api = client.ApiClient(configuration)
    return KubernetesClients(
        api=api,
        core=client.CoreV1Api(api),
        apps=client.AppsV1Api(api),
        batch=client.BatchV1Api(api),
        networking=client.NetworkingV1Api(api),
        authorization=client.AuthorizationV1Api(api),
        custom=client.CustomObjectsApi(api),
    )
