import json
import subprocess

import pytest

from crosscloud.provision import residue, residue_gcp


@pytest.mark.parametrize("prefix", ["ccx-r1", "ccx-0a9", "ccx-" + "a" * 16])
def test_check_name_prefix_accepts_ccx_run_prefix(prefix):
    residue.check_name_prefix(prefix)


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "ccx-",
        "ccx",
        "r1",
        "ccx-R1",
        "ccx-r_1",
        "ccx-r-1",
        "ccx-" + "a" * 17,
    ],
)
def test_check_name_prefix_rejects_anything_else(prefix):
    with pytest.raises(ValueError, match="ccx-"):
        residue.check_name_prefix(prefix)


_TOKEN = "ya29.canary-token-should-not-leak"
_SQL_URL = "https://sqladmin.googleapis.com/v1/projects/ghost-proj/instances"

# 우리 모듈이 만드는 자원 종류마다 gcloud 목록 명령 하나(실시간 조회).
# Cloud SQL 은 gcloud 가 REGION_UNREACHABLE 경고를 버려서 REST 로 따로 본다.
# (종류, gcloud 명령 단어, 고정 플래그, filter 대상 필드, 이름 필드 경로)
_LISTINGS = [
    ("run.googleapis.com/Service", "run services", (), "metadata.name"),
    (
        "artifactregistry.googleapis.com/Repository",
        "artifacts repositories",
        ("--location=all",),
        "name",
    ),
    ("secretmanager.googleapis.com/Secret", "secrets", (), "name"),
    ("iam.googleapis.com/ServiceAccount", "iam service-accounts", (), "email"),
    (
        "compute.googleapis.com/ForwardingRule",
        "compute forwarding-rules",
        (),
        "name",
    ),
    (
        "compute.googleapis.com/TargetHttpProxy",
        "compute target-http-proxies",
        (),
        "name",
    ),
    ("compute.googleapis.com/UrlMap", "compute url-maps", (), "name"),
    (
        "compute.googleapis.com/BackendService",
        "compute backend-services",
        (),
        "name",
    ),
    (
        "compute.googleapis.com/NetworkEndpointGroup",
        "compute network-endpoint-groups",
        (),
        "name",
    ),
    ("compute.googleapis.com/Address", "compute addresses", (), "name"),
    ("storage.googleapis.com/Bucket", "storage buckets", (), "name"),
]


def _done(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        [], returncode, stdout=stdout, stderr=stderr
    )


class FakeCommands:
    """gcloud·curl 호출을 기록하고 정해 둔 결과를 낸다.

    결과 키는 gcloud 목록이면 명령 단어(예: "secrets"), 토큰이면 "token",
    curl 이면 요청 URL 이다. 정하지 않은 gcloud 목록은 "[]", 토큰은 _TOKEN,
    curl 은 항목 없는 Cloud SQL 응답을 낸다. 문자열은 성공한 표준 출력이다.
    """

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, args, *, env, capture_output, text, check, **kwargs):
        assert capture_output and text and not check
        self.calls.append({"args": list(args), "env": env, **kwargs})
        key, default = _key(args)
        result = self.results.get(key, default)
        return _done(result) if isinstance(result, str) else result

    def gcloud_lists(self):
        return [c["args"] for c in self.calls if "list" in c["args"]]

    def curl_calls(self):
        return [c for c in self.calls if c["args"][0] == "curl"]


def _key(args):
    if args[0] == "curl":
        return args[-1], "{}"
    if args[1:3] == ["auth", "print-access-token"]:
        return "token", _TOKEN
    return " ".join(args[1 : args.index("list")]), "[]"


def _item(name_path, value):
    """이름 필드 경로(점 구분)에 value 를 둔 목록 항목 하나."""
    item = value
    for key in reversed(name_path.split(".")):
        item = {key: item}
    return item


def _scanner(fake):
    return residue_gcp.GcpResidueScanner(project="ghost-proj", run=fake)


def test_gcp_scan_lists_each_resource_kind_directly_across_locations():
    fake = FakeCommands()

    items = _scanner(fake).scan("ccx-r1")

    assert items == ()
    assert fake.gcloud_lists() == [
        [
            "gcloud",
            *words.split(),
            "list",
            "--project=ghost-proj",
            *flags,
            f"--filter={field}~ccx-r1",
            "--format=json",
        ]
        for _, words, flags, field in _LISTINGS
    ]
    for call in fake.calls:
        assert call["env"]["CLOUDSDK_COMPUTE_ALLOW_PARTIAL_ERROR"] == "false"


@pytest.mark.parametrize(
    ("kind", "words", "name_path"),
    [(kind, words, path) for kind, words, _, path in _LISTINGS],
)
def test_gcp_scan_finds_residue_of_each_listed_kind(kind, words, name_path):
    name = (
        "ccx-r1-run@ghost-proj.iam.gserviceaccount.com"
        if kind == residue_gcp.SERVICE_ACCOUNT
        else "projects/ghost-proj/locations/r/things/ccx-r1-x"
    )
    fake = FakeCommands({words: json.dumps([_item(name_path, name)])})

    items = _scanner(fake).scan("ccx-r1")

    assert items == (
        residue.ResidueItem(cloud="gcp", kind=kind, native_id=name),
    )


def test_gcp_scan_keeps_only_names_with_the_exact_prefix():
    names = [
        "projects/123/secrets/ccx-r1",
        "projects/123/secrets/ccx-r1-db-url",
        "projects/123/secrets/ccx-r12-db-url",
        "projects/123/secrets/team-ccx-r1",
    ]
    fake = FakeCommands({"secrets": json.dumps([{"name": n} for n in names])})

    items = _scanner(fake).scan("ccx-r1")

    assert [item.native_id for item in items] == names[:2]


def test_gcp_scan_matches_service_accounts_by_email_local_part():
    emails = [
        "ccx-r1-run@ghost-proj.iam.gserviceaccount.com",
        "ccx-r12-run@ghost-proj.iam.gserviceaccount.com",
        "team-ccx-r1@ghost-proj.iam.gserviceaccount.com",
    ]
    fake = FakeCommands(
        {"iam service-accounts": json.dumps([{"email": e} for e in emails])}
    )

    items = _scanner(fake).scan("ccx-r1")

    assert [item.native_id for item in items] == emails[:1]


@pytest.mark.parametrize(
    ("words", "stderr"),
    [
        (
            "artifacts repositories",
            "Listing items under project ghost-proj, across all locations.\n"
            "WARNING: Failed to list repositories for project ghost-proj",
        ),
        (
            "run services",
            "WARNING: The following Cloud Run regions did not respond: "
            "asia-northeast3. List results may be incomplete.",
        ),
        (
            "compute addresses",
            "Some requests did not succeed:\n - Internal error",
        ),
        ("secrets", "location asia-northeast3 is unreachable"),
    ],
)
def test_gcp_scan_partial_listing_warning_is_an_error(words, stderr):
    canary = "canary-detail-should-not-leak"
    fake = FakeCommands({words: _done("[]", f"{stderr} {canary}")})

    with pytest.raises(residue.ResidueScanError, match=words) as excinfo:
        _scanner(fake).scan("ccx-r1")

    assert canary not in str(excinfo.value)


def test_gcp_scan_allows_plain_status_lines_on_stderr():
    fake = FakeCommands(
        {
            "artifacts repositories": _done(
                "[]", "Listing items under project p, across all locations.\n"
            )
        }
    )

    assert _scanner(fake).scan("ccx-r1") == ()


def test_gcp_scan_lists_cloud_sql_through_rest_with_token_on_stdin():
    page1 = {"items": [{"name": "ccx-r1-db"}], "nextPageToken": "t2"}
    page2 = {"items": [{"name": "ccx-r12-db"}, {"name": "team-db"}]}
    fake = FakeCommands(
        {
            _SQL_URL: json.dumps(page1),
            f"{_SQL_URL}?pageToken=t2": json.dumps(page2),
        }
    )

    items = _scanner(fake).scan("ccx-r1")

    assert items == (
        residue.ResidueItem(
            cloud="gcp",
            kind="sqladmin.googleapis.com/Instance",
            native_id="ccx-r1-db",
        ),
    )
    curls = fake.curl_calls()
    assert [c["args"][-1] for c in curls] == [
        _SQL_URL,
        f"{_SQL_URL}?pageToken=t2",
    ]
    for call in curls:
        assert all(_TOKEN not in arg for arg in call["args"])
        assert call["input"] == f"Authorization: Bearer {_TOKEN}\n"


def test_gcp_scan_cloud_sql_region_unreachable_is_an_error():
    response = {
        "items": [],
        "warnings": [
            {"code": "REGION_UNREACHABLE", "message": "detail canary-x"}
        ],
    }
    fake = FakeCommands({_SQL_URL: json.dumps(response)})

    with pytest.raises(
        residue.ResidueScanError, match="REGION_UNREACHABLE"
    ) as e:
        _scanner(fake).scan("ccx-r1")

    assert "canary-x" not in str(e.value)


@pytest.mark.parametrize(
    ("key", "result"),
    [
        ("token", _done(stderr="ERROR: no active account", returncode=1)),
        (_SQL_URL, _done(stderr=f"curl: (22) 403 {_TOKEN}", returncode=22)),
        (_SQL_URL, "not json"),
        (_SQL_URL, "[]"),
        (_SQL_URL, '{"items": [{"id": 1}]}'),
        (_SQL_URL, '{"items": {}}'),
        (_SQL_URL, '{"warnings": ["odd"]}'),
    ],
)
def test_gcp_scan_cloud_sql_failure_is_an_error_without_token(key, result):
    fake = FakeCommands({key: result})

    with pytest.raises(residue.ResidueScanError, match="Cloud SQL") as e:
        _scanner(fake).scan("ccx-r1")

    assert _TOKEN not in str(e.value)


def test_gcp_scan_refuses_bad_prefix_before_querying():
    fake = FakeCommands()

    with pytest.raises(ValueError):
        _scanner(fake).scan("ccx")

    assert fake.calls == []


@pytest.mark.parametrize("words", ["secrets", "storage buckets"])
def test_gcp_scan_failure_of_any_listing_is_an_error(words):
    failure = _done(stderr="ERROR: API has not been used", returncode=1)
    fake = FakeCommands({words: failure})

    with pytest.raises(residue.ResidueScanError, match=words):
        _scanner(fake).scan("ccx-r1")


@pytest.mark.parametrize(
    "stdout",
    ["not json", '{"name": "x"}', "[1]", '[{"id": "x"}]', '[{"name": 1}]'],
)
def test_gcp_scan_unreadable_output_is_an_error(stdout):
    fake = FakeCommands({"compute url-maps": stdout})

    with pytest.raises(residue.ResidueScanError, match="compute url-maps"):
        _scanner(fake).scan("ccx-r1")


def test_gcp_scan_reads_nested_name_path():
    fake = FakeCommands({"run services": '[{"metadata": {"labels": {}}}]'})

    with pytest.raises(residue.ResidueScanError, match="metadata.name"):
        _scanner(fake).scan("ccx-r1")
