import datetime
import json

import pytest

from crosscloud import inventory_json, model
from crosscloud.discover import gcp

_PROJECT = "ghost-project-000000"
_REGION = "asia-northeast3"
_NOW = datetime.datetime(2026, 11, 7, 9, 0, tzinfo=datetime.UTC)
_DIGEST = "sha256:" + "c" * 64
_IMAGE = f"{_REGION}-docker.pkg.dev/{_PROJECT}/apps/request-api@{_DIGEST}"
_RUN = f"https://run.googleapis.com/v2/projects/{_PROJECT}/locations/{_REGION}"
_SQL = f"https://sqladmin.googleapis.com/v1/projects/{_PROJECT}/instances"
_SECRETS = (
    f"https://secretmanager.googleapis.com/v1/projects/{_PROJECT}/secrets"
)
_AR = (
    f"https://artifactregistry.googleapis.com/v1/projects/{_PROJECT}"
    f"/locations/{_REGION}/repositories"
)
_RULES = (
    f"https://compute.googleapis.com/compute/v1/projects/{_PROJECT}"
    "/global/forwardingRules"
)

_CANARIES = (
    "canary-pw-gcp-2b9c",
    "postgres://app:canary-gcp-url-pw@10.0.0.5:5432/shop",
    "Xk7canaryQ2Lm9Zv4Tn7Wk1Pb6Hs3Jd5Fg0",
)


class _FakeApi:
    """URL 별로 준비한 페이지를 돌려주는 가짜 GCP REST 전송."""

    def __init__(self, pages):
        self.pages = {url: list(responses) for url, responses in pages.items()}
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        response = self.pages[url].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _service(containers=None):
    return {
        "name": f"projects/{_PROJECT}/locations/{_REGION}/services/request-api",
        "uri": "https://request-api-xyz.a.run.app",
        "ingress": "INGRESS_TRAFFIC_ALL",
        "template": {
            "serviceAccount": f"app@{_PROJECT}.iam.gserviceaccount.com",
            "containers": containers
            or [
                {
                    "image": _IMAGE,
                    "env": [
                        {"name": "LOG_LEVEL", "value": "info"},
                        {"name": "DB_PASSWORD", "value": _CANARIES[0]},
                        {"name": "LEGACY_URL", "value": _CANARIES[1]},
                        {"name": "OPAQUE", "value": _CANARIES[2]},
                        {
                            "name": "DATABASE_URL",
                            "valueSource": {
                                "secretKeyRef": {
                                    "secret": f"projects/{_PROJECT}"
                                    "/secrets/app-db",
                                    "version": "3",
                                }
                            },
                        },
                    ],
                }
            ],
        },
    }


def _pages(services=None, **overrides):
    pages = {
        f"{_RUN}/services": [{"services": services or [_service()]}],
        _SQL: [
            {
                "items": [
                    {
                        "name": "app-db",
                        "region": _REGION,
                        "databaseVersion": "POSTGRES_16",
                        "settings": {
                            "tier": "db-f1-micro",
                            "ipConfiguration": {"ipv4Enabled": True},
                            "databaseFlags": [
                                {
                                    "name": "cloudsql.iam_authentication",
                                    "value": "on",
                                }
                            ],
                        },
                    },
                    {
                        "name": "mysql-db",
                        "region": _REGION,
                        "databaseVersion": "MYSQL_8_0",
                    },
                ]
            }
        ],
        _SECRETS: [
            {
                "secrets": [{"name": "projects/123/secrets/app-db"}],
                "nextPageToken": "t2",
            },
            {"secrets": [{"name": "projects/123/secrets/other"}]},
        ],
        _AR: [
            {
                "repositories": [
                    {"name": f"{_AR}/apps", "format": "DOCKER"},
                    {"name": f"{_AR}/npm", "format": "NPM"},
                ]
            }
        ],
        _RULES: [
            {
                "items": [
                    {
                        "name": "app-fr",
                        "loadBalancingScheme": "EXTERNAL_MANAGED",
                        "IPAddress": "34.1.2.3",
                    },
                    {"name": "classic-fr", "loadBalancingScheme": "EXTERNAL"},
                ]
            }
        ],
    }
    pages.update(overrides)
    return pages


def _read(api):
    reader = gcp.GcpInventoryReader(
        project=_PROJECT, region=_REGION, get_json=api, clock=lambda: _NOW
    )
    return reader.read()


@pytest.fixture
def api():
    return _FakeApi(_pages())


@pytest.fixture
def first_slice(api):
    return _read(api)


def test_reads_cloud_run_service_as_workload(first_slice):
    (workload,) = first_slice.workloads

    assert workload.name == "request-api"
    assert workload.cloud == "gcp"
    assert workload.platform == "cloud_run"
    assert workload.identity_id == f"app@{_PROJECT}.iam.gserviceaccount.com"
    assert workload.image == model.ImageRef(
        registry_repo=_IMAGE.split("@")[0], digest=_DIGEST
    )
    assert [entry.kind for entry in workload.entries] == ["http"]
    assert workload.secret_refs == (
        model.SecretRef(
            cloud="gcp",
            native_id=f"projects/{_PROJECT}/secrets/app-db",
            version="3",
        ),
    )


def test_env_goes_through_classify_env(first_slice):
    (workload,) = first_slice.workloads

    assert [(e.key, e.classification, e.value) for e in workload.env] == [
        ("LOG_LEVEL", "plain_nonsecret", "info"),
        ("DB_PASSWORD", "suspected_secret", None),
        ("LEGACY_URL", "suspected_secret", None),
        ("OPAQUE", "suspected_secret", None),
        ("DATABASE_URL", "secret_ref", None),
    ]


def test_reads_resources_and_follows_pages(first_slice):
    kinds = sorted(resource.kind for resource in first_slice.resources)

    assert kinds == [
        "container_registry",
        "load_balancer",
        "postgres_instance",
        "secret",
        "secret",
    ]
    (database,) = [
        r for r in first_slice.resources if r.kind == "postgres_instance"
    ]
    attributes = {kv.key: kv.value for kv in database.attributes}
    assert attributes["engine"] == "postgres"
    assert attributes["iam_auth"] is True
    assert attributes["public_ip"] is True


def test_unsupported_resources_become_omissions(first_slice):
    assert {o.subject for o in first_slice.omissions} == {
        f"sqladmin:projects/{_PROJECT}/instances/mysql-db",
        f"artifactregistry:{_AR}/npm",
        f"compute:projects/{_PROJECT}/global/forwardingRules/classic-fr",
        # 서비스 계정으로 닿는 대응표 밖 의존은 수집하지 않는다.
        f"identity_grants:app@{_PROJECT}.iam.gserviceaccount.com",
    }


def test_never_calls_secret_value_endpoints(api):
    _read(api)

    assert all(
        ":access" not in url and "/versions" not in url for url, _ in api.calls
    )
    assert (_SECRETS, {"pageToken": "t2"}) in api.calls


def test_no_env_secret_value_anywhere_in_inventory_json(first_slice):
    document = json.dumps(
        inventory_json.inventory_to_json(first_slice), ensure_ascii=False
    )

    for canary in _CANARIES:
        assert canary not in document
    assert "canary" not in document


def test_tag_image_and_multi_container_are_omitted():
    tagged = _service(containers=[{"image": "nginx:1.27"}])
    sidecar = dict(
        _service(containers=[{"image": _IMAGE}, {"image": _IMAGE}]),
        name="projects/p/locations/r/services/sidecar",
    )
    api = _FakeApi(_pages(services=[tagged, sidecar]))

    inventory = _read(api)

    assert inventory.workloads[0].image is None
    subjects = {o.subject for o in inventory.omissions}
    assert "image:request-api" in subjects
    assert "run:projects/p/locations/r/services/sidecar" in subjects


def test_permission_denied_section_becomes_omission():
    api = _FakeApi(_pages(**{_SQL: [gcp.GcpPermissionDeniedError(_SQL)]}))

    inventory = _read(api)

    assert (
        model.Omission(
            subject="sqladmin:*",
            detected_by="permission_denied",
            acknowledged_by=None,
        )
        in inventory.omissions
    )


def test_other_errors_propagate():
    api = _FakeApi(_pages(**{_SQL: [RuntimeError("boom")]}))

    with pytest.raises(RuntimeError, match="boom"):
        _read(api)


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _authorized(monkeypatch, response):
    class _Session:
        def __init__(self, credentials):
            self.credentials = credentials

        def get(self, url, params, timeout):
            return response

    monkeypatch.setattr(
        gcp.google.auth, "default", lambda scopes: ("creds", _PROJECT)
    )
    monkeypatch.setattr(gcp.auth_requests, "AuthorizedSession", _Session)
    return gcp.authorized_get_json()


def test_authorized_get_json_returns_body(monkeypatch):
    get_json = _authorized(monkeypatch, _Response(200, {"items": []}))

    assert get_json(_SQL, {}) == {"items": []}


def test_authorized_get_json_maps_forbidden(monkeypatch):
    get_json = _authorized(monkeypatch, _Response(403, {}))

    with pytest.raises(gcp.GcpPermissionDeniedError):
        get_json(_SQL, {})


def test_authorized_get_json_raises_other_failures(monkeypatch):
    get_json = _authorized(monkeypatch, _Response(500, {}))

    with pytest.raises(RuntimeError, match="500"):
        get_json(_SQL, {})


def test_secret_volume_is_recorded_and_omitted():
    service = _service()
    service["template"]["volumes"] = [
        {
            "name": "db",
            "secret": {
                "secret": f"projects/{_PROJECT}/secrets/db-url",
                "items": [{"version": "2", "path": "url"}],
            },
        },
        {"name": "scratch", "emptyDir": {}},
    ]
    api = _FakeApi(_pages(services=[service]))

    inventory = _read(api)

    assert "secret_volume:request-api/db" in {
        o.subject for o in inventory.omissions
    }
    assert (
        model.SecretRef(
            cloud="gcp",
            native_id=f"projects/{_PROJECT}/secrets/db-url",
            version="2",
        )
        in inventory.workloads[0].secret_refs
    )


def test_missing_service_account_means_default_compute_identity():
    service = _service()
    del service["template"]["serviceAccount"]
    api = _FakeApi(_pages(services=[service]))

    inventory = _read(api)

    assert (
        inventory.workloads[0].identity_id == "default_compute_service_account"
    )
    assert "identity_grants:default_compute_service_account" in {
        o.subject for o in inventory.omissions
    }
