"""SIMULATED integration: real planner/adapter/engine/journal, fake Google APIs.

Only in this test, unknown effective-policy contexts are asserted complete as a
test premise. These tests do not establish live policy completeness or access.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from migration.identity.adapters.gcp import GcpAdapter
from migration.identity.config import load_spec, read
from migration.identity.executor import execute, rollback_plan
from migration.identity.models import BlockedError, Inventory, digest
from migration.identity.planner import make_plan
from migration.identity.preflight import validate_plan
from migration.identity.state import State

from test_gcp import EMAIL, ROLE, Fake

EXAMPLES = Path(__file__).resolve().parents[2] / "identity-examples"


class SimulatedSource:
    def __init__(self, inventory):
        self.inventory = copy.deepcopy(inventory)

    def collect(self):
        return copy.deepcopy(self.inventory)


class IntegrationFake(Fake):
    def __init__(self):
        super().__init__()
        self.account = None
        self.role = None
        self.policy = {"etag": "reviewed-etag", "version": 3, "bindings": [
            {"role": "roles/storage.legacyBucketReader", "members": ["group:auditors@example.invalid"]},
            {"role": "roles/storage.objectViewer", "members": ["user:other@example.invalid"],
             "condition": {"title": "expires", "expression": "request.time < timestamp('2030-01-01T00:00:00Z')"}},
        ]}
        self.project_policy = {"bindings": [], "etag": "project-etag"}

    def invoke(self, path, kwargs):
        if path == "projects.getIamPolicy":
            self.calls.append((path, copy.deepcopy(kwargs)))
            return copy.deepcopy(self.project_policy)
        return super().invoke(path, kwargs)


class SimulatedCompleteGcpAdapter(GcpAdapter):
    def collect(self):
        inventory = super().collect()
        # SIMULATED PRECONDITION ONLY. Production collection retains all gaps.
        assert not any(v in {"denied", "failed", "unsupported"} for v in inventory["coverage"].values())
        inventory["coverage"] = {key: "complete" if value == "unknown" else value
                                 for key, value in inventory["coverage"].items()}
        inventory["limitations"].append("SIMULATED: complete policy-context premise; never a live collection result")
        return inventory


def setup_integration():
    spec = load_spec(EXAMPLES / "aws-gcp-readonly.yaml")
    source = SimulatedSource(read(EXAMPLES / "aws-gcp-inventory.json")["source"])
    fake = IntegrationFake()
    target = SimulatedCompleteGcpAdapter(spec.target, iam=fake.api, storage=fake.api, crm=fake.api)
    inventory = Inventory.from_dict({"schema_version": 1, "spec_digest": digest(spec),
                                    "source": source.collect(), "target": target.collect()})
    plan = make_plan(spec, inventory)
    validate_plan(plan)
    review = {"schema_version": 1, "plan_digest": plan.plan_digest,
              "reviewer": "simulated-reviewer", "reference": "mock-integration-only"}
    return plan, source, target, fake, review


def writes(fake):
    return [(path, kwargs) for path, kwargs in fake.calls
            if path in {"projects.serviceAccounts.create", "projects.roles.create", "buckets.setIamPolicy"}]


def test_gcp_adapter_engine_create_bind_and_restart_without_duplicate_writes(tmp_path):
    plan, source, target, fake, review = setup_integration()
    original_bindings = copy.deepcopy(fake.policy["bindings"])
    with State(tmp_path) as state:
        result = execute(plan, review, state, source, target)
        assert result["status"] == "applied_configuration"
        assert result["access_verified"] is False
        assert [r["status"] for r in result["operations"]] == ["applied"] * 3
    assert fake.account["email"] == EMAIL
    assert fake.role["name"] == ROLE
    assert fake.policy["bindings"][:2] == original_bindings
    assert fake.policy["bindings"][-1] == {"role": ROLE, "members": [f"serviceAccount:{EMAIL}"]}
    assert len(writes(fake)) == 3
    with State(tmp_path) as state:
        resumed = execute(plan, review, state, source, target)
        assert len(resumed["operations"]) == 3
    assert len(writes(fake)) == 3


def test_gcp_lost_bucket_write_response_reconciles_and_preserves_uncertain_ownership(tmp_path):
    plan, source, target, fake, review = setup_integration()
    fake.lose_response = True
    with State(tmp_path) as state:
        execute(plan, review, state, source, target)
        record = state.get(plan.plan_digest, plan.operations[-1].id)
        assert record["status"] == "reconciled"
        assert record["ownership"] == "uncertain"
        assert record["created"] is False
        assert rollback_plan(plan, state)["items"][0]["action"] == "manual_reconciliation_required"
    with State(tmp_path) as state:
        execute(plan, review, state, source, target)
    assert len(writes(fake)) == 3
    assert b"private-value" not in (tmp_path / "identity.sqlite3").read_bytes()


def test_gcp_external_project_policy_change_blocks_before_any_write(tmp_path):
    plan, source, target, fake, review = setup_integration()
    fake.project_policy = {"etag": "changed-project-etag", "bindings": [
        {"role": "roles/storage.objectAdmin", "members": [f"serviceAccount:{EMAIL}"]}]}
    with State(tmp_path) as state:
        with pytest.raises(BlockedError, match="target policy context"):
            execute(plan, review, state, source, target)
    assert writes(fake) == []


def test_gcp_restart_rejects_same_email_with_replaced_immutable_identity(tmp_path):
    plan, source, target, fake, review = setup_integration()
    with State(tmp_path) as state:
        execute(plan, review, state, source, target)
    fake.account["uniqueId"] = "100000000000000000002"
    with State(tmp_path) as state:
        with pytest.raises(BlockedError, match="drifted|changed"):
            execute(plan, review, state, source, target)
    assert len(writes(fake)) == 3
