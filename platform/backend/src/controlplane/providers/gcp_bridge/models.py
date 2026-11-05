"""Contract of the team's GCP API; it currently updates only the existing demo app."""

import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from controlplane.domain.models import Model
from controlplane.providers.settings import ProviderSettings


def github_repository(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.netloc != "github.com"
        or url.query
        or url.fragment
        or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*/?", url.path)
    ):
        raise ValueError("Public GitHub owner/repository URL required")
    return value.rstrip("/")


class RepositorySource(Model):
    kind: Literal["repository"] = "repository"
    repository: str = Field(max_length=2048)

    _repository = field_validator("repository")(github_repository)


class GcpRequest(Model):
    application: Literal["demo"] = "demo"
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    source: RepositorySource
    strategy: Literal["blue_green"] = "blue_green"


class GcpSettings(ProviderSettings):
    application: Literal["demo"] = "demo"
    environment_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    upstream_url: str = "http://127.0.0.1:8331"
    service_url: str
    allowed_repositories: list[str] = Field(min_length=1)
    poll_seconds: int = Field(default=3, ge=1, le=30)
    service_access: Literal["public", "authenticated"] = "authenticated"

    @field_validator("allowed_repositories")
    @classmethod
    def repositories(cls, values):
        return [github_repository(v) for v in values]

    @model_validator(mode="after")
    def endpoints(self):
        upstream = urlsplit(self.upstream_url)
        # Team API has no authentication: do not expose it or send requests to arbitrary URLs.
        if (
            upstream.scheme != "http"
            or upstream.hostname not in {"127.0.0.1", "localhost", "::1"}
            or not upstream.port
            or upstream.username
            or upstream.password
            or upstream.path not in {"", "/"}
            or upstream.query
            or upstream.fragment
        ):
            raise ValueError("GCP upstream must be an operator-owned loopback HTTP service")
        service = urlsplit(self.service_url)
        if (
            service.scheme != "https"
            or not service.hostname
            or service.username
            or service.password
            or service.path not in {"", "/"}
            or service.query
            or service.fragment
        ):
            raise ValueError("Explicit expected HTTPS service origin required")
        self.upstream_url = self.upstream_url.rstrip("/")
        self.service_url = self.service_url.rstrip("/")
        return self

    @property
    def environment(self):
        return self

    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json", exclude={"database_url"}), sort_keys=True
            ).encode()
        ).hexdigest()

    def validate_request(self, request):
        if (request.application, request.environment_id) != (
            self.application,
            self.environment_id,
        ) or request.source.repository not in self.allowed_repositories:
            raise ValueError("Unregistered app/environment/repository")
