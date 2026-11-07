import dataclasses
import datetime
import json

import pytest

from crosscloud import cli, model
from crosscloud.discover import env

_CANARY = "canary-cli-pw-4e1d"


def _inventory(cloud="aws"):
    return model.Inventory(
        cloud=cloud,
        collected_at=datetime.datetime(2026, 11, 7, tzinfo=datetime.UTC),
        resources=(),
        workloads=(
            model.Workload(
                name="request-api",
                cloud=cloud,
                platform="ecs" if cloud == "aws" else "cloud_run",
                native_id="svc",
                identity_id="role",
                image=None,
                code_repo=None,
                entries=(
                    model.Entry(kind="http", source_id="tg", attributes=()),
                ),
                env=(env.classify_env("DB_PASSWORD", _CANARY),),
                secret_refs=(),
                db_user=None,
                depends_on=(),
            ),
        ),
        grants=(),
        restrictions=(),
        restriction_scans=(),
        couplings=(),
        omissions=(),
        attestation=model.CollectionAttestation(
            secret_value_calls=None,
            collector_can_read_secret_values=True,
            cai_viewer_bound=False,
            suspected_values_retained=0,
            evidence_ref="not_collected",
        ),
    )


class _Reader:
    def __init__(self, inventory):
        self.inventory = inventory

    def read(self):
        return self.inventory


def _run(argv, capsys, inventory=None):
    made = []

    def make_reader(args):
        made.append(args)
        return _Reader(inventory or _inventory())

    code = cli.main(argv, make_reader=make_reader)
    captured = capsys.readouterr()
    return code, captured, made


def test_discover_prints_inventory_and_report(capsys):
    code, captured, made = _run(
        ["discover", "--direction", "aws_to_gcp", "--region", "r"], capsys
    )

    assert code == 0
    document = json.loads(captured.out)
    assert document["inventory"]["cloud"] == "aws"
    assert document["report"]["direction"] == "aws_to_gcp"
    assert [d["reason"] for d in document["report"]["decisions"]] == [
        "plaintext_secret_in_env"
    ]
    assert made[0].region == "r"


def test_discover_output_never_contains_env_secret(capsys, tmp_path):
    output = tmp_path / "discover.json"

    code, captured, _ = _run(
        [
            "discover",
            "--direction",
            "aws_to_gcp",
            "--region",
            "r",
            "--output",
            str(output),
        ],
        capsys,
    )

    assert code == 0
    assert _CANARY not in captured.out + captured.err
    assert _CANARY not in output.read_text(encoding="utf-8")


def test_repo_scan_adds_couplings(capsys, tmp_path):
    (tmp_path / "config.js").write_text(
        "require('@aws-sdk/rds-signer');\n", encoding="utf-8"
    )

    code, captured, _ = _run(
        [
            "discover",
            "--direction",
            "aws_to_gcp",
            "--region",
            "r",
            "--repo",
            f"request-api={tmp_path}",
        ],
        capsys,
    )

    assert code == 0
    inventory = json.loads(captured.out)["inventory"]
    assert [c["rule_id"] for c in inventory["couplings"]] == [
        "aws-rds-signer-import"
    ]
    assert inventory["workloads"][0]["code_repo"] == str(tmp_path)


def test_repo_for_unknown_workload_fails(capsys, tmp_path):
    code, captured, _ = _run(
        [
            "discover",
            "--direction",
            "aws_to_gcp",
            "--region",
            "r",
            "--repo",
            f"nope={tmp_path}",
        ],
        capsys,
    )

    assert code == 1
    assert "nope" in captured.err
    assert captured.out == ""


def test_schema_sql_feeds_support_limits(capsys, tmp_path):
    schema = tmp_path / "schema.sql"
    schema.write_text("CREATE TABLE t (id int);", encoding="utf-8")
    database = model.Resource(
        kind="postgres_instance",
        cloud="aws",
        native_id="db-1",
        region="r",
        attributes=(model.KeyValue("engine", "postgres"),),
        source_of_truth="api",
    )
    inventory = dataclasses.replace(_inventory(), resources=(database,))

    code, captured, _ = _run(
        [
            "discover",
            "--direction",
            "aws_to_gcp",
            "--region",
            "r",
            "--schema-sql",
            str(schema),
        ],
        capsys,
        inventory=inventory,
    )

    assert code == 0
    decisions = json.loads(captured.out)["report"]["decisions"]
    assert {"subject": "db-1", "signal": "missing_primary_key: public.t"} in [
        {"subject": d["subject"], "signal": d["signal"]} for d in decisions
    ]


def test_gcp_direction_requires_project(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(
            ["discover", "--direction", "gcp_to_aws", "--region", "r"],
            make_reader=lambda args: _Reader(_inventory("gcp")),
        )

    assert excinfo.value.code == 2
    assert "--project" in capsys.readouterr().err


def test_rejects_unknown_direction(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["discover", "--direction", "aws_to_azure", "--region", "r"])

    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    ("direction", "project", "reader_type"),
    [
        ("aws_to_gcp", None, "AwsInventoryReader"),
        ("gcp_to_aws", "p", "GcpInventoryReader"),
    ],
)
def test_default_reader_follows_direction(
    monkeypatch, direction, project, reader_type
):
    monkeypatch.setattr(
        cli.gcp, "authorized_get_json", lambda: lambda url, params: {}
    )
    args = cli.build_parser().parse_args(
        ["discover", "--direction", direction, "--region", "r"]
        + (["--project", project] if project else [])
    )

    assert type(cli.make_default_reader(args)).__name__ == reader_type
