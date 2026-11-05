"""Argo execution binding. Worker must reach private cluster DNS and public HTTPS."""

import httpx

from controlplane.execution.onprem.settings import ArgoSettings
from controlplane.providers.onprem import ArgoProvider, manifests
from controlplane.providers.onprem.connection import connect
from controlplane.providers.onprem.health_http import build_health_client, serves_release
from controlplane.providers.onprem.probe_job import observe as observe_probe

PROBE_TIMEOUT = httpx.Timeout(5, connect=3)


class ArgoRuntime:
    def __init__(self, settings: ArgoSettings):
        self.settings = settings
        self.clients = None
        self.provider = None
        self.http = None

    def close(self):
        if self.http:
            self.http.close()
        if self.clients:
            self.clients.close()

    def connect(self):
        if self.clients is None:
            self.clients = connect(
                kubeconfig=self.settings.kubeconfig,
                context=self.settings.context,
                expected_server=self.settings.server,
            )
            self.provider = ArgoProvider(self.clients, self.settings.environment)
            self.http = build_health_client(
                cafile=self.settings.health_ca_file, timeout=PROBE_TIMEOUT
            )

    def _origin(self, *, production: bool) -> str:
        env = self.settings.environment
        if production:
            return f"https://{env.hostname}"
        return f"http://{manifests.name(env)}-preview.{env.namespace}.svc.{env.cluster_domain}"

    def probe(self, request, release, *, production, probe_id=None):
        if not production and self.settings.preview_probe_mode == "job":
            return observe_probe(
                self.provider,
                request,
                release,
                image=self.settings.preview_probe_image,
                probe_id=probe_id,
            )
        try:
            with self.http.stream(
                "GET", self._origin(production=production) + request.health_path
            ) as response:
                return serves_release(response, release.value)
        except httpx.HTTPError:
            return False
