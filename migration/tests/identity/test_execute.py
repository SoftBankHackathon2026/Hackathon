from __future__ import annotations

import copy
import fcntl

import pytest
from migration.identity.common import IdentityError, digest, private_write
from migration.identity.execute import (
    apply,
    ordered,
    preflight,
    rollback,
    rollback_plan,
    status,
)
from migration.identity.verify import verify
from tests.identity.conftest import MemoryTargets


def test_apply_resume_is_idempotent_and_private(
    identity_config, identity_plan, memory_targets, tmp_path
):
    prepared = preflight(identity_config, identity_plan, memory_targets)
    first = apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    assert first["status"] == "complete"
    writes = memory_targets.writes.copy()
    second = apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    assert second["status"] == "complete" and memory_targets.writes == writes
    assert all(op["state"] == "applied" for op in status(tmp_path)["operations"])
    assert (tmp_path / "identity.sqlite3").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "identity.lock").stat().st_mode & 0o777 == 0o600


def test_lost_response_reconciles_without_duplicate_or_claiming_creation(
    identity_config, identity_plan, memory_targets, tmp_path
):
    prepared = preflight(identity_config, identity_plan, memory_targets)
    rid = identity_plan["resources"][0]["id"]
    memory_targets.fail_after_write = rid
    result = apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    assert result["status"] == "partial" and result["results"][0]["state"] == "unknown"
    assert (
        apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)[
            "status"
        ]
        == "complete"
    )
    assert memory_targets.writes.count(rid) == 1
    first = next(x for x in status(tmp_path)["operations"] if x["id"] == rid)
    assert first["created"] == 0


def test_existing_equal_resources_are_not_owned(
    identity_config, identity_plan, memory_targets, tmp_path
):
    for r in identity_plan["resources"]:
        memory_targets.items[r["id"]] = r["payload"]
    prepared = preflight(identity_config, identity_plan, memory_targets)
    apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    assert not memory_targets.writes
    assert rollback_plan(identity_config, identity_plan, tmp_path)["revoke"] == []


def test_preflight_conflict_and_post_preflight_drift(
    identity_config, identity_plan, memory_targets, tmp_path
):
    prepared = preflight(identity_config, identity_plan, memory_targets)
    rid = identity_plan["resources"][0]["id"]
    memory_targets.items[rid] = {"unrelated": True}
    assert (
        preflight(identity_config, identity_plan, memory_targets)["status"] == "blocked"
    )
    assert (
        apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)[
            "results"
        ][0]["reason"]
        == "TARGET_CHANGED_SINCE_PREFLIGHT"
    )
    assert not memory_targets.writes


def test_tampered_and_cross_plan_preflight_blocked(
    identity_config, identity_plan, memory_targets, tmp_path
):
    prepared = preflight(identity_config, identity_plan, memory_targets)
    prepared["snapshots"].clear()
    with pytest.raises(IdentityError, match="PREFLIGHT_HASH_MISMATCH"):
        apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    prepared["preflight_hash"] = digest(
        {k: v for k, v in prepared.items() if k != "preflight_hash"}
    )
    with pytest.raises(IdentityError, match="INCOMPLETE_PREFLIGHT"):
        apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)


def test_process_lock_and_state_plan_binding(
    identity_config, identity_plan, memory_targets, tmp_path
):
    prepared = preflight(identity_config, identity_plan, memory_targets)
    with (tmp_path / "identity.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(IdentityError, match="MIGRATION_ALREADY_RUNNING"):
            apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    other = copy.deepcopy(identity_plan)
    other["mapping_hash"] = "different"
    other["plan_hash"] = digest({k: v for k, v in other.items() if k != "plan_hash"})
    prepared = preflight(identity_config, other, memory_targets)
    with pytest.raises(IdentityError, match="STATE_BELONGS_TO_ANOTHER_PLAN"):
        apply(identity_config, other, prepared, tmp_path, memory_targets)


def test_rollback_revokes_only_new_grants_and_cannot_resume(
    identity_config, identity_plan, memory_targets, tmp_path
):
    prepared = preflight(identity_config, identity_plan, memory_targets)
    apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    proposal = rollback_plan(identity_config, identity_plan, tmp_path)
    assert any(r.startswith("kc_realm:") for r in proposal["retain"])
    result = rollback(identity_config, identity_plan, tmp_path, memory_targets)
    assert result["status"] == "complete" and set(memory_targets.deleted) == set(
        proposal["revoke"]
    )
    for _ in range(2):
        with pytest.raises(IdentityError, match="ROLLED_BACK_PLAN_CANNOT_RESUME"):
            apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    assert not set(proposal["revoke"]) & set(memory_targets.items)


class FlakyReads(MemoryTargets):
    def __init__(self, failing):
        super().__init__()
        self.failing = set(failing)

    def read(self, resource):
        if resource["id"] in self.failing:
            self.failing.discard(resource["id"])
            raise IdentityError("TARGET_TRANSPORT_ERROR", status="failed")
        return super().read(resource)


def test_failed_read_on_resume_keeps_created_grant_revocable(
    identity_config, identity_plan, tmp_path
):
    targets = FlakyReads(())
    prepared = preflight(identity_config, identity_plan, targets)
    apply(identity_config, identity_plan, prepared, tmp_path, targets)
    revoke = rollback_plan(identity_config, identity_plan, tmp_path)["revoke"]
    targets.failing = {revoke[0]}
    result = apply(identity_config, identity_plan, prepared, tmp_path, targets)
    assert result["status"] == "partial"
    assert result["results"][-1] == {
        "id": revoke[0],
        "state": "applied",
        "reason": "TARGET_TRANSPORT_ERROR",
    }
    assert rollback_plan(identity_config, identity_plan, tmp_path)["revoke"] == revoke


def test_rollback_keeps_revoking_after_one_failure(
    identity_config, identity_plan, tmp_path
):
    targets = FlakyReads(())
    prepared = preflight(identity_config, identity_plan, targets)
    apply(identity_config, identity_plan, prepared, tmp_path, targets)
    revoke = rollback_plan(identity_config, identity_plan, tmp_path)["revoke"]
    targets.failing = {revoke[0]}
    result = rollback(identity_config, identity_plan, tmp_path, targets)
    assert result["status"] == "partial"
    assert result["results"][0] == {
        "id": revoke[0],
        "state": "unknown",
        "reason": "TARGET_TRANSPORT_ERROR",
    }
    assert set(targets.deleted) == set(revoke[1:])
    assert (
        rollback_plan(identity_config, identity_plan, tmp_path)["revoke"] == revoke[:1]
    )


def test_dependency_cycle_rejected():
    with pytest.raises(IdentityError, match="INVALID_RESOURCE_DEPENDENCIES"):
        list(
            ordered(
                [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}]
            )
        )


def test_infra_result_checks_endpoint_not_admin_status(
    identity_config, identity_plan, memory_targets, tmp_path
):
    file = tmp_path / "infra.json"
    private_write(
        file,
        {
            "scope": "infrastructure_only",
            "status": "complete",
            "connection": {
                "base_url": "https://elsewhere.test",
                "realm": "master",
                "admin_access": "not_checked",
            },
            "checks": [{"name": "signing_keys", "status": "pass"}],
        },
    )
    identity_config["targets"]["keycloak"]["infra_result"] = str(file)
    identity_plan["config_hash"] = digest(identity_config)
    identity_plan["plan_hash"] = digest(
        {k: v for k, v in identity_plan.items() if k != "plan_hash"}
    )
    with pytest.raises(IdentityError, match="KEYCLOAK_INFRA_ENDPOINT_MISMATCH"):
        preflight(identity_config, identity_plan, memory_targets)


def test_configuration_success_is_not_migration_success(
    identity_config, identity_plan, memory_targets, tmp_path
):
    prepared = preflight(identity_config, identity_plan, memory_targets)
    apply(identity_config, identity_plan, prepared, tmp_path, memory_targets)
    result = verify(identity_config, identity_plan, memory_targets)
    assert result["configuration_status"] == "complete"
    assert result["access_status"] == "not_run" and result["status"] == "partial"
    assert len(result["missing_access_coverage"]) == 4


def test_access_probes_require_positive_and_negative_per_target(
    identity_config, identity_plan, memory_targets
):
    memory_targets.items = {r["id"]: r["payload"] for r in identity_plan["resources"]}
    group = identity_plan["mapping_rows"][0]["target_group"]
    check = {
        "kind": "kubernetes",
        "group": group,
        "token_env": "USER_TOKEN",
        "expected_subject": "kc-user-uuid",
        "allowed": True,
        "namespace": "orders-dev",
        "verb": "get",
        "resource": "pods",
        "api_group": "",
    }
    result = verify(
        identity_config,
        identity_plan,
        memory_targets,
        {"schema_version": 1, "checks": [check]},
        probes={"kubernetes": lambda c, conf: "real_token_fixture"},
    )
    assert len(result["missing_access_coverage"]) == 3
    assert result["status"] == "partial"


def test_symlink_state_refused(
    identity_config, identity_plan, memory_targets, tmp_path
):
    db = tmp_path / "identity.sqlite3"
    db.symlink_to(tmp_path / "unrelated.sqlite")
    with pytest.raises(IdentityError, match="STATE_FILE_SYMLINK"):
        apply(
            identity_config,
            identity_plan,
            preflight(identity_config, identity_plan, memory_targets),
            tmp_path,
            memory_targets,
        )
