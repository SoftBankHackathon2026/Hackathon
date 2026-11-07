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
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, args, *, capture_output, text, check):
        assert capture_output and text and not check
        self.calls.append(list(args))
        return self.result


def _assets(*names):
    return json.dumps(
        [
            {"name": name, "assetType": f"type/{i}"}
            for i, name in enumerate(names)
        ]
    )


def _scanner(fake):
    return residue_gcp.GcpResidueScanner(project="ghost-proj", run=fake)


def test_gcp_scan_runs_one_asset_search_scoped_to_project():
    fake = FakeGcloud(
        subprocess.CompletedProcess([], 0, stdout="[]", stderr="")
    )

    items = _scanner(fake).scan("ccx-r1")

    assert items == ()
    (args,) = fake.calls
    assert args == [
        "gcloud",
        "asset",
        "search-all-resources",
        "--scope=projects/ghost-proj",
        '--query=name:"ccx-r1"',
        "--format=json",
    ]


def test_gcp_scan_keeps_only_resources_named_with_the_prefix():
    stdout = _assets(
        "//sqladmin.googleapis.com/projects/ghost-proj/instances/ccx-r1-db",
        "//run.googleapis.com/projects/p/locations/r/services/ccx-r1",
        "//run.googleapis.com/projects/p/locations/r/services/ccx-r12-app",
        "//compute.googleapis.com/projects/p/global/addresses/team-ccx-r1",
    )
    fake = FakeGcloud(
        subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    )

    items = _scanner(fake).scan("ccx-r1")

    assert items == (
        residue.ResidueItem(
            cloud="gcp",
            kind="type/0",
            native_id=(
                "//sqladmin.googleapis.com/projects/ghost-proj/instances/"
                "ccx-r1-db"
            ),
        ),
        residue.ResidueItem(
            cloud="gcp",
            kind="type/1",
            native_id="//run.googleapis.com/projects/p/locations/r/services/"
            "ccx-r1",
        ),
    )


def test_gcp_scan_refuses_bad_prefix_before_querying():
    fake = FakeGcloud(None)

    with pytest.raises(ValueError):
        _scanner(fake).scan("ccx")

    assert fake.calls == []


def test_gcp_scan_failure_is_an_error_not_a_clean_result():
    fake = FakeGcloud(
        subprocess.CompletedProcess(
            [], 1, stdout="", stderr="ERROR: cloudasset.googleapis.com disabled"
        )
    )

    with pytest.raises(residue.ResidueScanError, match="cloudasset"):
        _scanner(fake).scan("ccx-r1")


@pytest.mark.parametrize("stdout", ["not json", '{"name": "x"}', "[1]"])
def test_gcp_scan_unreadable_output_is_an_error(stdout):
    fake = FakeGcloud(
        subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    )

    with pytest.raises(residue.ResidueScanError):
        _scanner(fake).scan("ccx-r1")
