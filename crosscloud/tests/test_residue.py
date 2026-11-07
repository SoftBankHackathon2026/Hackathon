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


class FakeGcloud:
    """gcloud 호출을 기록하고, 질의 종류(asset·iam)별로 회차마다 결과를 낸다.

    결과 목록의 마지막 값은 그 뒤 회차에도 계속 쓴다. 문자열은 성공한
    표준 출력이고, CompletedProcess 는 그대로 돌려준다.
    """

    def __init__(self, assets=("[]",), accounts=("[]",)):
        self.results = {"asset": list(assets), "iam": list(accounts)}
        self.calls = []

    def __call__(self, args, *, capture_output, text, check):
        assert capture_output and text and not check
        self.calls.append(list(args))
        queue = self.results[args[1]]
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, str):
            return subprocess.CompletedProcess([], 0, stdout=result, stderr="")
        return result


class FakeSleep:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


def _assets(*names, asset_type="type/x"):
    return json.dumps(
        [{"name": name, "assetType": asset_type} for name in names]
    )


def _accounts(*emails):
    return json.dumps(
        [
            {
                "email": email,
                "name": f"projects/ghost-proj/serviceAccounts/{email}",
                "uniqueId": "1234567890",
            }
            for email in emails
        ]
    )


def _failure(stderr):
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


def _scanner(fake, sleep=None):
    return residue_gcp.GcpResidueScanner(
        project="ghost-proj", run=fake, sleep=sleep or FakeSleep()
    )


_SQL = "//sqladmin.googleapis.com/projects/ghost-proj/instances/ccx-r1-db"


def test_gcp_scan_queries_assets_and_service_accounts_scoped_to_project():
    fake = FakeGcloud()

    _scanner(fake).scan("ccx-r1")

    assert fake.calls[:2] == [
        [
            "gcloud",
            "asset",
            "search-all-resources",
            "--scope=projects/ghost-proj",
            '--query=name:"ccx-r1"',
            "--format=json",
        ],
        [
            "gcloud",
            "iam",
            "service-accounts",
            "list",
            "--project=ghost-proj",
            "--filter=email~^ccx-r1[-@]",
            "--format=json",
        ],
    ]


def test_gcp_scan_confirms_clean_only_after_repeated_empty_rounds():
    fake = FakeGcloud()
    sleep = FakeSleep()

    items = _scanner(fake, sleep).scan("ccx-r1")

    assert items == ()
    assert [args[1] for args in fake.calls] == ["asset", "iam"] * 3
    assert sleep.waits == [60, 60]


def test_gcp_scan_reports_residue_that_appears_after_index_delay():
    fake = FakeGcloud(assets=("[]", _assets(_SQL)))
    sleep = FakeSleep()

    items = _scanner(fake, sleep).scan("ccx-r1")

    assert items == (
        residue.ResidueItem(cloud="gcp", kind="type/x", native_id=_SQL),
    )
    assert sleep.waits == [60]


def test_gcp_scan_returns_residue_at_once_without_waiting():
    fake = FakeGcloud(assets=(_assets(_SQL),))
    sleep = FakeSleep()

    items = _scanner(fake, sleep).scan("ccx-r1")

    assert len(items) == 1
    assert sleep.waits == []


def test_gcp_scan_keeps_only_resources_named_with_the_prefix():
    fake = FakeGcloud(
        assets=(
            _assets(
                _SQL,
                "//run.googleapis.com/projects/p/locations/r/services/ccx-r1",
                "//run.googleapis.com/projects/p/locations/r/services/"
                "ccx-r12-app",
                "//compute.googleapis.com/projects/p/global/addresses/"
                "team-ccx-r1",
            ),
        )
    )

    items = _scanner(fake).scan("ccx-r1")

    assert [item.native_id for item in items] == [
        _SQL,
        "//run.googleapis.com/projects/p/locations/r/services/ccx-r1",
    ]


def test_gcp_scan_finds_service_account_named_by_numeric_id_via_email():
    numeric = (
        "//iam.googleapis.com/projects/ghost-proj/serviceAccounts/1234567890"
    )
    fake = FakeGcloud(
        assets=(_assets(numeric, asset_type=residue_gcp.SERVICE_ACCOUNT),),
        accounts=(
            _accounts(
                "ccx-r1-run@ghost-proj.iam.gserviceaccount.com",
                "ccx-r12-run@ghost-proj.iam.gserviceaccount.com",
                "team-ccx-r1@ghost-proj.iam.gserviceaccount.com",
            ),
        ),
    )

    items = _scanner(fake).scan("ccx-r1")

    assert items == (
        residue.ResidueItem(
            cloud="gcp",
            kind=residue_gcp.SERVICE_ACCOUNT,
            native_id="projects/ghost-proj/serviceAccounts/"
            "ccx-r1-run@ghost-proj.iam.gserviceaccount.com",
        ),
    )


def test_gcp_scan_refuses_bad_prefix_before_querying():
    fake = FakeGcloud()

    with pytest.raises(ValueError):
        _scanner(fake).scan("ccx")

    assert fake.calls == []


@pytest.mark.parametrize(
    ("assets", "accounts", "match"),
    [
        (_failure("ERROR: cloudasset disabled"), "[]", "cloudasset"),
        ("[]", _failure("ERROR: iam.serviceAccounts.list denied"), "denied"),
    ],
)
def test_gcp_scan_failure_is_an_error_not_a_clean_result(
    assets, accounts, match
):
    fake = FakeGcloud(assets=(assets,), accounts=(accounts,))

    with pytest.raises(residue.ResidueScanError, match=match):
        _scanner(fake).scan("ccx-r1")


@pytest.mark.parametrize(
    "stdout", ["not json", '{"name": "x"}', "[1]", '[{"name": "x"}]']
)
@pytest.mark.parametrize("query", ["asset", "iam"])
def test_gcp_scan_unreadable_output_is_an_error(stdout, query):
    results = {"assets": ("[]",), "accounts": ("[]",)}
    results["assets" if query == "asset" else "accounts"] = (stdout,)
    fake = FakeGcloud(**results)

    with pytest.raises(residue.ResidueScanError):
        _scanner(fake).scan("ccx-r1")
