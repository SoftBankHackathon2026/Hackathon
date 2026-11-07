import os

import pytest
from migration.secrets.__main__ import main
from migration.secrets.access import access_report
from migration.secrets.common import SecretsError, load, save

from .conftest import PATH


def test_cli_offline_plan_template_and_private_outputs(scenario, capsys):
    s = scenario
    config, inv, mapping, output = [
        str(s.directory / name)
        for name in ("config.yaml", "inventory.json", "mapping.json", "plan.json")
    ]
    for path, value in ((config, s.config), (inv, s.inventory), (mapping, s.mapping)):
        save(path, value)
    assert (
        main(
            [
                "plan",
                "--config",
                config,
                "--inventory",
                inv,
                "--mapping",
                mapping,
                "--out",
                output,
            ]
        )
        == 0
    )
    assert (
        load(output)["plan_hash"] == s.plan["plan_hash"]
        and not os.stat(output).st_mode & 0o077
    )
    assert "identity_policy_documents" not in capsys.readouterr().out
    assert (
        main(
            [
                "mapping-template",
                "--config",
                config,
                "--inventory",
                inv,
                "--out",
                output,
            ]
        )
        == 0
    )
    assert load(output)["bindings"][0]["identity_assignments"] == []
    assert (
        main(["plan", "--config", config, "--inventory", inv, "--mapping", mapping])
        == 2
    )
    assert (
        main(
            [
                "plan",
                "--config",
                config,
                "--inventory",
                inv,
                "--mapping",
                mapping,
                "--out",
                mapping,
            ]
        )
        == 2
    )


def test_duplicate_keys_public_credential_and_symlink_inputs_rejected(
    tmp_path, monkeypatch, capsys
):
    from migration.secrets.common import credential, json_value

    f = tmp_path / "token"
    f.write_text("PRIVATE-TOKEN")
    f.chmod(0o644)
    with pytest.raises(SecretsError, match="PRIVATE_FILE"):
        credential("file:" + str(f))
    link = tmp_path / "link"
    link.symlink_to(f)
    with pytest.raises(SecretsError):
        credential("file:" + str(link))
    with pytest.raises(SecretsError, match="DUPLICATE"):
        json_value('{"one":1,"one":2}')
    with pytest.raises(SecretsError):
        json_value('{"x":NaN}')
    assert main(["validate", "--config", str(f)]) == 2
    assert "PRIVATE-TOKEN" not in capsys.readouterr().out


def access_inputs(s):
    ip = s.identity_plan
    application = {
        "schema_version": 1,
        "operation": "apply",
        "plan_hash": ip["plan_hash"],
        "status": "complete",
        "results": [{"id": r["id"], "state": "applied"} for r in ip["resources"]],
    }
    checks = {
        "schema_version": 1,
        "checks": [
            {
                "kind": "openbao",
                "group": "orders-reader",
                "expected_subject": "user-uuid",
                "role": "reader",
                "allowed": True,
                "path": "kv/data/" + PATH,
            },
            {
                "kind": "openbao",
                "group": "orders-reader",
                "expected_subject": "user-uuid",
                "role": "reader",
                "allowed": False,
                "path": "kv/data/unrelated",
            },
        ],
    }
    verification = {
        "schema_version": 1,
        "operation": "verify",
        "plan_hash": ip["plan_hash"],
        "configuration": [{"id": r["id"], "status": "pass"} for r in ip["resources"]],
        "access": [
            {
                "index": i,
                "kind": "openbao",
                "group": "orders-reader",
                "expected_allowed": check["allowed"],
                "status": "pass",
            }
            for i, check in enumerate(checks["checks"])
        ],
    }
    return application, verification, checks


def test_access_evidence_requires_exact_secret_and_same_subject_allow_deny(scenario):
    s = scenario
    a, v, c = access_inputs(s)
    assert access_report(s.config, s.plan, a, v, c)["access_status"] == "verified"
    c["checks"][1]["expected_subject"] = "another-user"
    assert access_report(s.config, s.plan, a, v, c)["access_status"] == "incomplete"
    c["checks"][0]["path"] = "kv/data/different-secret"
    assert access_report(s.config, s.plan, a, v, c)["access_status"] == "incomplete"


@pytest.mark.parametrize(
    "change", ["plan", "role", "applied", "configuration", "index"]
)
def test_access_receipt_mismatch_is_blocked(scenario, change):
    s = scenario
    a, v, c = access_inputs(s)
    if change == "plan":
        v["plan_hash"] = "other"
    elif change == "role":
        c["checks"][0]["role"] = "unrelated"
    elif change == "applied":
        a["results"] = []
    elif change == "configuration":
        v["configuration"] = []
    else:
        v["access"][0]["index"] = -1
    with pytest.raises(SecretsError):
        access_report(s.config, s.plan, a, v, c)
