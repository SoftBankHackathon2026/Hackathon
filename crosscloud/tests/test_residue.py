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


# 우리 모듈이 만드는 자원 종류마다 서비스 API 목록 명령 하나(실시간 조회).
# (종류, gcloud 명령 단어, 고정 플래그, filter 대상 필드, 이름 필드 경로)
_LISTINGS = [
    ("sqladmin.googleapis.com/Instance", "sql instances", (), "name", "name"),
    (
        "run.googleapis.com/Service",
        "run services",
        ("--region=asia-northeast3",),
        "metadata.name",
        "metadata.name",
    ),
    (
        "artifactregistry.googleapis.com/Repository",
        "artifacts repositories",
        ("--location=asia-northeast3",),
        "name",
        "name",
    ),
    ("secretmanager.googleapis.com/Secret", "secrets", (), "name", "name"),
    (
        "iam.googleapis.com/ServiceAccount",
        "iam service-accounts",
        (),
        "email",
        "email",
    ),
    (
        "compute.googleapis.com/ForwardingRule",
        "compute forwarding-rules",
        (),
        "name",
        "name",
    ),
    (
        "compute.googleapis.com/TargetHttpProxy",
        "compute target-http-proxies",
        (),
        "name",
        "name",
    ),
    ("compute.googleapis.com/UrlMap", "compute url-maps", (), "name", "name"),
    (
        "compute.googleapis.com/BackendService",
        "compute backend-services",
        (),
        "name",
        "name",
    ),
    (
        "compute.googleapis.com/NetworkEndpointGroup",
        "compute network-endpoint-groups",
        (),
        "name",
        "name",
    ),
    ("compute.googleapis.com/Address", "compute addresses", (), "name", "name"),
    ("storage.googleapis.com/Bucket", "storage buckets", (), "name", "name"),
]


class FakeGcloud:
    """gcloud 목록 호출을 기록하고 명령 단어(예: "sql instances")별 결과를 낸다.

    정하지 않은 명령은 빈 목록("[]")을 낸다. 문자열은 성공한 표준 출력이고,
    CompletedProcess 는 그대로 돌려준다.
    """

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def __call__(self, args, *, capture_output, text, check):
        assert capture_output and text and not check
        self.calls.append(list(args))
        result = self.results.get(_words(args), "[]")
        if isinstance(result, str):
            return subprocess.CompletedProcess([], 0, stdout=result, stderr="")
        return result


def _words(args):
    """gcloud 다음부터 'list' 앞까지의 명령 단어."""
    return " ".join(args[1 : args.index("list")])


def _item(name_path, value):
    """이름 필드 경로(점 구분)에 value 를 둔 목록 항목 하나."""
    item = value
    for key in reversed(name_path.split(".")):
        item = {key: item}
    return item


def _scanner(fake):
    return residue_gcp.GcpResidueScanner(
        project="ghost-proj", region="asia-northeast3", run=fake
    )


def test_gcp_scan_lists_each_resource_kind_directly_once():
    fake = FakeGcloud()

    items = _scanner(fake).scan("ccx-r1")

    assert items == ()
    expected = [
        [
            "gcloud",
            *words.split(),
            "list",
            "--project=ghost-proj",
            *flags,
            f"--filter={field}~ccx-r1",
            "--format=json",
        ]
        for _, words, flags, field, _ in _LISTINGS
    ]
    assert fake.calls == expected


@pytest.mark.parametrize(
    ("kind", "words", "name_path"),
    [(kind, words, name_path) for kind, words, _, _, name_path in _LISTINGS],
)
def test_gcp_scan_finds_residue_of_each_kind(kind, words, name_path):
    name = (
        "ccx-r1-run@ghost-proj.iam.gserviceaccount.com"
        if kind == residue_gcp.SERVICE_ACCOUNT
        else "projects/ghost-proj/locations/r/things/ccx-r1-x"
    )
    fake = FakeGcloud({words: json.dumps([_item(name_path, name)])})

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
    fake = FakeGcloud({"secrets": json.dumps([{"name": n} for n in names])})

    items = _scanner(fake).scan("ccx-r1")

    assert [item.native_id for item in items] == names[:2]


def test_gcp_scan_matches_service_accounts_by_email_local_part():
    emails = [
        "ccx-r1-run@ghost-proj.iam.gserviceaccount.com",
        "ccx-r12-run@ghost-proj.iam.gserviceaccount.com",
        "team-ccx-r1@ghost-proj.iam.gserviceaccount.com",
    ]
    fake = FakeGcloud(
        {"iam service-accounts": json.dumps([{"email": e} for e in emails])}
    )

    items = _scanner(fake).scan("ccx-r1")

    assert [item.native_id for item in items] == emails[:1]


def test_gcp_scan_refuses_bad_prefix_before_querying():
    fake = FakeGcloud()

    with pytest.raises(ValueError):
        _scanner(fake).scan("ccx")

    assert fake.calls == []


@pytest.mark.parametrize("words", ["sql instances", "storage buckets"])
def test_gcp_scan_failure_of_any_listing_is_an_error(words):
    failure = subprocess.CompletedProcess(
        [], 1, stdout="", stderr="ERROR: API has not been used in project"
    )
    fake = FakeGcloud({words: failure})

    with pytest.raises(residue.ResidueScanError, match=words):
        _scanner(fake).scan("ccx-r1")


@pytest.mark.parametrize(
    "stdout",
    ["not json", '{"name": "x"}', "[1]", '[{"id": "x"}]', '[{"name": 1}]'],
)
def test_gcp_scan_unreadable_output_is_an_error(stdout):
    fake = FakeGcloud({"compute url-maps": stdout})

    with pytest.raises(residue.ResidueScanError, match="compute url-maps"):
        _scanner(fake).scan("ccx-r1")


def test_gcp_scan_reads_nested_name_path():
    fake = FakeGcloud({"run services": '[{"metadata": {"labels": {}}}]'})

    with pytest.raises(residue.ResidueScanError, match="metadata.name"):
        _scanner(fake).scan("ccx-r1")
