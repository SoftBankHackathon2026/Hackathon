from __future__ import annotations

import json
from pathlib import Path

from migration.identity.__main__ import main
from migration.identity.common import digest, load, private_write
from migration.identity.plan import create_plan


def test_offline_cli_mapping_plan_report_export(
    identity_config, identity_inventory, identity_mapping, tmp_path, capsys
):
    files = {
        name: tmp_path / (name + ".json")
        for name in ("config", "inventory", "mapping", "plan", "export")
    }
    for name, obj in (
        ("config", identity_config),
        ("inventory", identity_inventory),
        ("mapping", identity_mapping),
    ):
        private_write(files[name], obj)
    assert (
        main(
            [
                "plan",
                "--config",
                str(files["config"]),
                "--inventory",
                str(files["inventory"]),
                "--mapping",
                str(files["mapping"]),
                "--out",
                str(files["plan"]),
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "planned" and output["artifact_written"]
    assert "resources" not in output
    report = tmp_path / "review.md"
    assert (
        main(
            [
                "report",
                "--config",
                str(files["config"]),
                "--plan",
                str(files["plan"]),
                "--out",
                str(report),
            ]
        )
        == 0
    )
    assert "주체" in report.read_text() and "not_run" in report.read_text()
    assert report.stat().st_mode & 0o777 == 0o600
    assert (
        main(
            [
                "export",
                "--config",
                str(files["config"]),
                "--plan",
                str(files["plan"]),
                "--out",
                str(files["export"]),
            ]
        )
        == 3
    )
    assert len(load(files["export"])["handoffs"]) == 3


def test_mapping_exit_three_artifact_is_valid_plan_input(
    identity_inventory, tmp_path, capsys
):
    inventory, mapping = tmp_path / "inventory.json", tmp_path / "mapping.json"
    private_write(inventory, identity_inventory)
    assert main(["mapping", "--inventory", str(inventory), "--out", str(mapping)]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "review_required"
    assert set(load(mapping)) == {
        "schema_version",
        "inventory_hash",
        "bindings",
        "excluded_assignments",
    }


def test_blocked_plan_can_be_reported_but_not_applied(
    identity_config, identity_inventory, identity_mapping, tmp_path, capsys
):
    identity_mapping["bindings"][0]["review_ref"] = ""
    plan = create_plan(identity_config, identity_inventory, identity_mapping)
    cf, pf = tmp_path / "config.json", tmp_path / "plan.json"
    private_write(cf, identity_config)
    private_write(pf, plan)
    assert (
        main(
            [
                "report",
                "--config",
                str(cf),
                "--plan",
                str(pf),
                "--out",
                str(tmp_path / "blocked.md"),
            ]
        )
        == 0
    )
    assert "MAPPING_REVIEW_REQUIRED" in (tmp_path / "blocked.md").read_text()
    capsys.readouterr()
    assert (
        main(
            [
                "preflight",
                "--config",
                str(cf),
                "--plan",
                str(pf),
                "--out",
                str(tmp_path / "preflight.json"),
            ]
        )
        == 2
    )
    assert "PLAN_BLOCKED" in capsys.readouterr().out


def test_secret_validation_errors_never_echo_input(tmp_path, capsys):
    file = tmp_path / "config.json"
    private_write(file, {"schema_version": 1, "password": "VERY-SECRET-INPUT"})
    assert (
        main(["collect", "--config", str(file), "--out", str(tmp_path / "output.json")])
        == 2
    )
    text = capsys.readouterr().out
    assert "VERY-SECRET-INPUT" not in text and "Traceback" not in text
    assert not (tmp_path / "output.json").exists()


def test_wrong_confirmed_plan_does_not_resolve_credentials(
    identity_config, identity_plan, tmp_path, capsys
):
    cf, pf = tmp_path / "config.json", tmp_path / "plan.json"
    private_write(cf, identity_config)
    private_write(pf, identity_plan)
    assert (
        main(
            [
                "apply",
                "--config",
                str(cf),
                "--plan",
                str(pf),
                "--preflight",
                "/not/read",
                "--state-dir",
                str(tmp_path / "state"),
                "--confirm-plan",
                "wrong",
            ]
        )
        == 2
    )
    assert "CONFIRMED_PLAN_MISMATCH" in capsys.readouterr().out


def test_report_refuses_other_plan_verification(
    identity_config, identity_plan, tmp_path, capsys
):
    cf, pf, vf = [tmp_path / (n + ".json") for n in ("config", "plan", "verify")]
    for path, value in (
        (cf, identity_config),
        (pf, identity_plan),
        (vf, {"operation": "verify", "plan_hash": "wrong"}),
    ):
        private_write(path, value)
    assert (
        main(
            [
                "report",
                "--config",
                str(cf),
                "--plan",
                str(pf),
                "--verification",
                str(vf),
                "--out",
                str(tmp_path / "report.md"),
            ]
        )
        == 2
    )
    assert "REPORT_PLAN_MISMATCH" in capsys.readouterr().out


def test_shipped_examples_are_valid_and_hash_bound():
    root = Path(__file__).resolve().parents[2] / "identity-examples"
    from migration.identity.common import validate_config

    config = validate_config(load(root / "config.example.json"))
    inv = load(root / "inventory.example.json")
    mapping = load(root / "mapping.example.json")
    plan = create_plan(config, inv, mapping)
    assert plan["status"] == "planned" and plan["plan_hash"] == digest(
        {k: v for k, v in plan.items() if k != "plan_hash"}
    )
