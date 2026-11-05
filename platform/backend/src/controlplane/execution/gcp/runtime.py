import time
from urllib.parse import urlsplit

import httpx
from google.auth import impersonated_credentials
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import run_v2
from google.oauth2 import id_token

from controlplane.execution.gcp.apps import GcpAppProvisioner
from controlplane.execution.gcp.auth import credentials as gcp_credentials
from controlplane.execution.gcp.cache import GcpCacheProvisioner
from controlplane.execution.gcp.data import GcpDataProvisioner
from controlplane.execution.gcp.migration import GcpMigrationRunner
from controlplane.execution.gcp.redis import GcpRedisConnector
from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.gcp.origin import is_registered_origin
from controlplane.providers.gcp.provider import CloudRunProvider

RELEASE_HEADER = "X-Launchpad-Release"


class GcpRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.provider: CloudRunProvider | None = None
        self.http: httpx.Client | None = None
        self.credentials = None
        self.data: GcpDataProvisioner | None = None
        self.migrate: GcpMigrationRunner | None = None
        self.cache: GcpCacheProvisioner | None = None
        self.app: GcpAppProvisioner | None = None
        self.redis: GcpRedisConnector | None = None
        self.now = time.time

    def connect(self):
        if self.provider is not None:
            return
        credentials = gcp_credentials(self.settings)
        self.credentials = credentials
        if self.settings.data_provisioning:
            self.data = GcpDataProvisioner(AuthorizedSession(credentials), self.settings)
            self.migrate = GcpMigrationRunner(self.data.session, self.settings)
        if self.settings.cache_provisioning:
            self.cache = GcpCacheProvisioner(AuthorizedSession(credentials), self.settings)
            self.redis = GcpRedisConnector(self.cache.session, self.settings)
        self.provider = CloudRunProvider(
            run_v2.ServicesClient(credentials=credentials),
            run_v2.RevisionsClient(credentials=credentials),
            self.settings.environment,
            self.settings.tracing,
        )
        if self.settings.catalog_dynamic:
            self.app = GcpAppProvisioner(
                AuthorizedSession(credentials), self.settings, self.provider
            )
        self.http = httpx.Client(
            timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False
        )

    def probe(self, request, service, *, url, release):
        if not url:
            return False
        self._check_origin(url)
        headers = self._auth_headers(service)
        try:
            with self.http.stream("GET", url + request.health_path, headers=headers) as response:
                return response.status_code == 200 and (
                    release is None or response.headers.get(RELEASE_HEADER) == release
                )
        except httpx.HTTPError:
            return False

    def _check_origin(self, url):
        if not is_registered_origin(url):
            raise ProviderConflict("Invalid probe origin")
        hostname = urlsplit(url).hostname
        if url != self.settings.environment.service_url and not hostname.endswith(".run.app"):
            raise ProviderConflict("Unregistered probe origin")

    def _auth_headers(self, service):
        if service.invoker_iam_disabled or self.settings.service_access == "public":
            return {}
        if isinstance(self.credentials, impersonated_credentials.Credentials):
            identity = impersonated_credentials.IDTokenCredentials(
                self.credentials, target_audience=service.uri, include_email=True
            )
            identity.refresh(Request())
            token = identity.token
        else:
            token = id_token.fetch_id_token(Request(), service.uri)
        return {"Authorization": "Bearer " + token}

    def close(self):
        if self.app:
            self.app.session.close()
        if self.cache:
            self.cache.session.close()
        if self.data:
            self.data.session.close()
        if self.http:
            self.http.close()
        if self.provider:
            self.provider.services.transport.close()
            self.provider.revisions.transport.close()
