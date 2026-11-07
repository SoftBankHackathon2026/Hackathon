import copy
import json

import pytest
from migration.secrets.common import SecretsError, digest, save, seal_artifact
from migration.secrets.engine import report, run
from migration.secrets.models import SecretValue
from migration.secrets.plan import build_plan, preflight
from migration.secrets.state import State, StateError
from migration.secrets.target import decode

from .conftest import ARN, CANARY, PATH


def test_copy_previous_current_bytes_no_old_history_and_idempotent_resume(scenario):
    s = scenario
    s.source.values["v1"] = SecretValue(b"\x00\xff\x12", "binary")
    s.source.values["v2"] = SecretValue(
        b'{ "password": "same", "port": 5432 }', "string"
    )
    with State(s.state) as state:
        result = run(s.config, s.plan, s.prepared, s.source, s.target, state)
        assert (
            result["copy_status"] == "verified"
            and result["migration_complete"] is False
        )
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    doc, _ = s.target.docs[PATH]
    assert set(doc["values"]) == {"v1", "v2"} and s.target.writes == 1
    assert "v0" not in s.source.reads
    for v in ("v1", "v2"):
        assert decode(doc["values"][v]) == s.source.values[v]
    assert CANARY not in s.state.read_bytes()


@pytest.mark.parametrize("phase", ["before", "after"])
def test_response_loss_never_duplicates_or_guesses_success(scenario, phase):
    s = scenario
    s.target.crash = phase
    with State(s.state) as state:
        with pytest.raises(SecretsError):
            run(s.config, s.plan, s.prepared, s.source, s.target, state)
        assert state.status()["operations"]["unknown"] == 1
        if phase == "after":
            run(s.config, s.plan, s.prepared, s.source, s.target, state)
            assert s.target.writes == 1
        else:
            with pytest.raises(SecretsError, match="WRITE_UNKNOWN"):
                run(s.config, s.plan, s.prepared, s.source, s.target, state)
            assert s.target.writes == 0


def test_unowned_existing_equal_data_never_adopted(scenario):
    s = scenario
    s.target.docs[PATH] = ({"value": "PRIVATE-SECRET-CANARY"}, 1)
    assert preflight(s.config, s.plan, s.source, s.target)["status"] == "blocked"
    with State(s.state) as state, pytest.raises(SecretsError, match="OWNED"):
        run(s.config, s.plan, s.prepared, s.source, s.target, state)


def test_source_changes_during_read_blocks_write(scenario):
    s = scenario
    s.source.change_during_read = True
    with State(s.state) as state, pytest.raises(SecretsError, match="SOURCE_CHANGED"):
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    assert s.target.writes == 0


def test_mount_recreation_blocks_writes(scenario):
    s = scenario
    s.target.observed["accessor"] = "replacement"
    with State(s.state) as state, pytest.raises(SecretsError, match="TARGET_REPLACED"):
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    assert s.target.writes == 0


def test_missing_fresh_collection_scope_blocks_even_with_same_resources(scenario):
    s = scenario
    s.source.data["coverage"] = [
        {"scope": "selected_secret_listing", "status": "unknown"}
    ]
    with State(s.state) as state, pytest.raises(SecretsError, match="SOURCE_CHANGED"):
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    assert s.target.writes == 0


def test_corrupt_destination_fails_fresh_verification(scenario):
    s = scenario
    with State(s.state) as state:
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
        s.target.docs[PATH][0]["values"]["v2"]["payload"] = "Y29ycnVwdA=="
        with pytest.raises(SecretsError, match="VERIFICATION_FAILED"):
            run(
                s.config,
                s.plan,
                s.prepared,
                s.source,
                s.target,
                state,
                verify_only=True,
            )
        assert report(state)["copy_status"] == "incomplete"


@pytest.mark.parametrize(
    "feature,expected",
    [
        ("scheduled_deletion", "LIFECYCLE"),
        ("managed_by_service", "LIFECYCLE"),
        ("kms", "KMS_POLICY"),
        ("resource_policy", "RESTRICTION"),
    ],
)
def test_unpreserved_requirements_block_plan_and_keep_evidence(
    scenario, feature, expected
):
    s = scenario
    item = s.source.data["resources"][0]
    item[feature] = (
        {"coverage": "unknown"}
        if feature == "kms"
        else {
            "Statement": [{"Effect": "Deny", "Condition": {"StringEquals": {"x": "y"}}}]
        }
        if feature == "resource_policy"
        else True
    )
    inv = s.source.inventory()
    s.mapping["inventory_hash"] = inv["inventory_hash"]
    p = build_plan(s.config, inv, s.mapping)
    assert p["status"] == "blocked" and any(
        expected in b["code"] for b in p["blockers"]
    )
    assert (
        p["policy_evidence"][0][feature] == item[feature]
        if feature in {"kms", "resource_policy"}
        else True
    )


def test_policy_documents_subjects_and_future_operations_preserved(scenario):
    p = scenario.plan
    assert p["policy_evidence"][0]["identity_policy_documents"] == [
        scenario.identity_inventory["policies"][0]
    ]
    row = p["rows"][0]
    assert (
        row["source"]["arn"] == ARN and row["subjects"][0]["principal_id"] == "g-stable"
    )
    assert row["omitted_versions"] == ["v0"]
    assert all(v["automatic_grant"] is False for v in row["operations"].values())
    assert set(row["operations"]) == {
        "read",
        "create",
        "update",
        "delete",
        "restore",
        "destroy",
        "manage",
    }


def test_kms_restriction_is_preserved_and_blocks_automatic_access(scenario):
    s = scenario
    s.source.data["resources"][0]["kms"] = {
        "id": "key-id",
        "coverage": "collected",
        "policy": {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "kms:Decrypt",
                    "Condition": {"StringEquals": {"aws:PrincipalTag/team": "orders"}},
                }
            ]
        },
    }
    inv = s.source.inventory()
    s.mapping["inventory_hash"] = inv["inventory_hash"]
    plan = build_plan(s.config, inv, s.mapping)
    assert plan["status"] == "blocked"
    assert "SOURCE_KMS_POLICY_RESTRICTION_UNSUPPORTED" in {
        b["code"] for b in plan["blockers"]
    }
    assert plan["policy_evidence"][0]["kms"]["policy"]["Statement"][0]["Condition"] == {
        "StringEquals": {"aws:PrincipalTag/team": "orders"}
    }


def test_no_previous_and_equal_current_previous_are_explicit(scenario):
    s = scenario
    s.source.data["resources"][0]["versions"] = [{"id": "v2", "stages": ["AWSCURRENT"]}]
    inv = s.source.inventory()
    s.mapping["inventory_hash"] = inv["inventory_hash"]
    assert (
        build_plan(s.config, inv, s.mapping)["rows"][0]["previous_status"]
        == "not_present"
    )
    s.source.data["resources"][0]["versions"][0]["stages"].append("AWSPREVIOUS")
    inv = s.source.inventory()
    s.mapping["inventory_hash"] = inv["inventory_hash"]
    assert set(
        build_plan(s.config, inv, s.mapping)["rows"][0]["versions"].values()
    ) == {"v2"}


def test_same_name_recreated_source_and_changed_principal_are_not_adopted(scenario):
    s = scenario
    s.source.data["resources"][0]["arn"] = ARN + "replacement"
    inv = s.source.inventory()
    s.mapping["inventory_hash"] = inv["inventory_hash"]
    with pytest.raises(SecretsError, match="UNKNOWN_BINDING"):
        build_plan(s.config, inv, s.mapping)
    s.identity_plan["mapping_rows"][0]["source"]["principal_id"] = "g-new"
    seal_artifact(s.identity_plan, "plan_hash")
    save(s.config["identity"]["plan"], s.identity_plan)
    with (
        State(s.state) as state,
        pytest.raises(SecretsError, match="IDENTITY_INPUT_CHANGED"),
    ):
        run(s.config, s.plan, s.prepared, s.source, s.target, state)


@pytest.mark.parametrize(
    "mutation",
    [
        "hash",
        "path",
        "business",
        "account",
        "duplicate",
        "pending",
        "config",
        "preparation",
    ],
)
def test_plan_input_binding_guards(scenario, mutation):
    s = scenario
    if mutation == "hash":
        s.mapping["inventory_hash"] = "bad"
    elif mutation in {"path", "business"}:
        s.mapping["bindings"][0][
            "target_path" if mutation == "path" else "business"
        ] = "migration/wrong"
    elif mutation == "account":
        s.config["source"]["account_id"] = "999988887777"
    elif mutation == "duplicate":
        s.mapping["bindings"].append(copy.deepcopy(s.mapping["bindings"][0]))
    elif mutation == "pending":
        s.source.data["resources"][0]["versions"][0]["stages"] = ["AWSPENDING"]
        s.inventory = s.source.inventory()
        s.mapping["inventory_hash"] = s.inventory["inventory_hash"]
        assert build_plan(s.config, s.inventory, s.mapping)["status"] == "blocked"
        return
    elif mutation == "config":
        s.identity_config["targets"]["openbao"]["base_url"] = "https://other.test"
        save(s.config["identity"]["config"], s.identity_config)
    else:
        save(s.config["preparation"]["verify_result"], {"status": "failed"})
    with pytest.raises(SecretsError):
        build_plan(s.config, s.inventory, s.mapping)


def test_destination_lock_coordinates_different_state_paths(scenario):
    s = scenario
    with State(s.state) as one, State(s.directory / "second.sqlite3") as two:
        with one.lock("destination"), pytest.raises(StateError, match="LOCKED"):
            with two.lock("destination"):
                pass


def test_reports_never_contain_values(scenario):
    s = scenario
    with State(s.state) as state:
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
        serialized = json.dumps(report(state))
    assert CANARY.decode() not in serialized and CANARY not in s.state.read_bytes()
    assert digest(s.config) in s.state.read_bytes().decode("utf-8", errors="ignore")


def test_explicitly_deferred_identity_allows_copy_without_grant_or_access_success(
    scenario,
):
    s = scenario
    from migration.secrets.common import validate_config

    s.config.pop("identity")
    validate_config(s.config)
    s.mapping["bindings"][0].update(access_mode="deferred", identity_assignments=[])
    s.plan = build_plan(s.config, s.inventory, s.mapping)
    s.prepared = preflight(s.config, s.plan, s.source, s.target)
    assert s.plan["identity_status"] == "deferred_not_verified"
    assert s.plan["rows"][0]["subjects"] == []
    assert (
        s.plan["rows"][0]["operations"]["read"]["status"] == "deferred_not_implemented"
    )
    with State(s.state) as state:
        result = run(s.config, s.plan, s.prepared, s.source, s.target, state)
    assert result["copy_status"] == "verified" and result["migration_complete"] is False
    assert "requires_identity" in result["access_status"]


def test_missing_identity_cannot_silently_claim_linked_access(scenario):
    s = scenario
    s.config.pop("identity")
    with pytest.raises(SecretsError, match="IDENTITY_ASSIGNMENTS_REQUIRED"):
        build_plan(s.config, s.inventory, s.mapping)
    s.mapping["bindings"][0]["access_mode"] = "deferred"
    with pytest.raises(SecretsError, match="DEFERRED_ACCESS"):
        build_plan(s.config, s.inventory, s.mapping)


def test_wrong_configuration_never_poison_another_verified_journal(scenario):
    s = scenario
    with State(s.state) as state:
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
        other = copy.deepcopy(s.config)
        other["migration_id"] = "different-job"
        with pytest.raises(SecretsError, match="CONFIG_CHANGED"):
            run(other, s.plan, s.prepared, s.source, s.target, state)
        assert state.status()["job"]["stage"] == "verified"


def rotate_source(s):
    s.source.values["v3"] = SecretValue(b"rotated-at-source", "string")
    s.source.data["resources"][0]["versions"] = [
        {"id": "v1", "stages": []},
        {"id": "v2", "stages": ["AWSPREVIOUS"]},
        {"id": "v3", "stages": ["AWSCURRENT"]},
    ]
    inv = s.source.inventory()
    s.mapping["inventory_hash"] = inv["inventory_hash"]
    return build_plan(s.config, inv, s.mapping)


def test_replan_after_source_rotation_updates_its_own_earlier_copy(scenario, tmp_path):
    s = scenario
    with State(s.state) as state:
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    plan = rotate_source(s)
    prepared = preflight(s.config, plan, s.source, s.target)
    binding = plan["rows"][0]["binding_id"]
    assert (prepared["status"], prepared["conflicts"], prepared["supersedes"]) == (
        "ready",
        [],
        {binding: 1},
    )
    with State(tmp_path / "second.sqlite3") as state:
        assert (
            run(s.config, plan, prepared, s.source, s.target, state)["copy_status"]
            == "verified"
        )
        run(s.config, plan, prepared, s.source, s.target, state)
    doc, version = s.target.docs[PATH]
    assert (version, s.target.writes) == (2, 2)
    assert (doc["current"], doc["previous"]) == ("v3", "v2")
    assert decode(doc["values"]["v3"]).data == b"rotated-at-source"


def test_replan_never_supersedes_rotated_or_foreign_documents(scenario):
    s = scenario
    with State(s.state) as state:
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    plan = rotate_source(s)
    doc, version = s.target.docs[PATH]
    for change in (
        {"rotation": {"phase": "complete"}},
        {"pending": "op"},
        {"owner": "other"},
    ):
        s.target.docs[PATH] = ({**doc, **change}, version)
        prepared = preflight(s.config, plan, s.source, s.target)
        assert (prepared["status"], prepared["supersedes"]) == ("blocked", {})
    s.target.docs[PATH] = (
        {**doc, "source": {**doc["source"], "arn": "other"}},
        version,
    )
    assert preflight(s.config, plan, s.source, s.target)["status"] == "blocked"


def test_superseded_version_must_match_preflight(scenario, tmp_path):
    s = scenario
    with State(s.state) as state:
        run(s.config, s.plan, s.prepared, s.source, s.target, state)
    plan = rotate_source(s)
    prepared = preflight(s.config, plan, s.source, s.target)
    doc, version = s.target.docs[PATH]
    s.target.docs[PATH] = (doc, version + 1)
    with (
        State(tmp_path / "second.sqlite3") as state,
        pytest.raises(SecretsError, match="NOT_OWNED_BY_JOURNAL"),
    ):
        run(s.config, plan, prepared, s.source, s.target, state)
    assert s.target.writes == 1
