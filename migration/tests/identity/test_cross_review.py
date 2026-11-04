"""Independent integration checks for planner/provider boundaries, entirely offline."""
from dataclasses import replace

import pytest

from migration.identity.adapters.base import empty_inventory, observation, observation_key
from migration.identity.adapters.kubernetes import KubernetesAdapter
from migration.identity.config import read, write
from migration.identity.models import (BlockedError, ConfigError, Inventory, MigrationSpec,
                                       digest, primitive)
from migration.identity.planner import COVERAGE, make_plan
from migration.identity.preflight import validate_plan


def namespace_spec_dict():
    return {
        "schema_version": 1,
        "direction": "keycloak-to-kubernetes",
        "workload": "namespace-read",
        "source": {
            "provider": "keycloak", "scope": "demo", "identity": "group-readers-id", "resource": "/readers",
            "options": {"url": "https://identity.example.invalid", "version": "26.4.2"},
        },
        "target": {
            "provider": "kubernetes", "scope": "reports", "identity": "oidc:readers", "resource": "reports",
            "options": {"url": "https://cluster.example.invalid:6443", "context": "isolated-test", "group_claim_confirmed": True},
        },
        "allowed_changes": ["kubernetes.role", "kubernetes.role_binding"],
    }


def namespace_fixture():
    spec = MigrationSpec.from_dict(namespace_spec_dict())
    source, target = empty_inventory(spec.source), empty_inventory(spec.target)
    for side in (source, target):
        side["coverage"] = {key: "complete" for key in COVERAGE}
    source["identities"] = [{"stable_id": "group-readers-id", "name": "/readers", "kind": "group"}]
    source["policies"] = [{"kind": "kubernetes.rules", "rules": [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}]}]
    for kind in ("kubernetes.role", "kubernetes.role_binding"):
        target["observations"][observation_key(kind, "reports/migration-namespace-read")] = observation()
    return spec, Inventory.from_dict({"schema_version": 1, "spec_digest": digest(spec), "source": source, "target": target})


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(schema_version=True),
    lambda raw: raw.update(unrecognized="ignored-by-unsafe-parser"),
    lambda raw: raw["source"].update(unrecognized="not-an-endpoint-field"),
    lambda raw: raw["target"]["options"].update(group_claim_confirmed="true"),
    lambda raw: raw["target"]["options"].update(context={"name": "wrong-type"}),
])
def test_spec_rejects_unknown_fields_and_ambiguous_types(mutation):
    raw = namespace_spec_dict()
    mutation(raw)
    with pytest.raises(ConfigError):
        MigrationSpec.from_dict(raw)


def test_secret_field_rejected_without_value_in_error_or_file(tmp_path):
    secret = "private-test-sentinel-never-write"
    raw = namespace_spec_dict()
    raw["target"]["options"]["access_token"] = secret
    with pytest.raises(ConfigError) as error:
        MigrationSpec.from_dict(raw)
    assert secret not in str(error.value)
    destination = tmp_path / "must-not-exist.json"
    with pytest.raises(ConfigError):
        write(destination, raw)
    assert not destination.exists()


@pytest.mark.parametrize("content", ["schema_version: 1\nschema_version: 2\n", "1: value\n", "recursive: &node [*node]\n"])
def test_artifact_loader_rejects_duplicate_nonstring_and_recursive_input(tmp_path, content):
    path = tmp_path / "malformed.yaml"
    path.write_text(content)
    with pytest.raises(ConfigError):
        read(path)


@pytest.mark.parametrize("field", ["organization", "boundary", "resource_policy"])
def test_source_unknown_coverage_blocks_execution_even_with_operations(field):
    spec, inventory = namespace_fixture()
    inventory.source["coverage"][field] = "unknown"
    plan = make_plan(spec, inventory)
    assert any(f"source coverage {field}: unknown" == reason for reason in plan.blockers)
    with pytest.raises(BlockedError):
        validate_plan(plan)


def test_planner_defaults_match_kubernetes_adapter_contract():
    spec, inventory = namespace_fixture()
    plan = make_plan(spec, inventory)
    assert not plan.blockers
    validate_plan(plan)
    assert [op.resource for op in plan.operations] == ["reports/migration-namespace-read"] * 2
    assert plan.operations[1].depends_on == [plan.operations[0].id]
    assert plan.operations[1].desired == {"role_name": "migration-namespace-read", "group": "oidc:readers"}

    class NotFound(Exception):
        status = 404

    class ReadOnlyEmptyAPI:
        calls = 0

        def read_namespaced_role(self, *, name, namespace):
            self.calls += 1
            assert (name, namespace) == ("migration-namespace-read", "reports")
            raise NotFound()

        read_namespaced_role_binding = read_namespaced_role

    api = ReadOnlyEmptyAPI()
    adapter = KubernetesAdapter(spec.target, api)
    for operation in plan.operations:
        assert adapter.observe(operation) == operation.expected == observation()
    assert api.calls == 2


def test_unapproved_binding_kind_is_blocked_by_allowlist():
    spec, inventory = namespace_fixture()
    spec = replace(spec, allowed_changes=["kubernetes.role"])
    inventory = replace(inventory, spec_digest=digest(spec))
    plan = make_plan(spec, inventory)
    assert "operation is outside allowed_changes: kubernetes.role_binding" in plan.blockers
    with pytest.raises(BlockedError):
        validate_plan(plan)


def test_keycloak_group_membership_does_not_invent_pod_read_permissions():
    spec, inventory = namespace_fixture()
    inventory.source["policies"] = [{"kind": "group_role_mappings", "id": "group-readers-id", "realmRoles": ["reader"]}]
    plan = make_plan(spec, inventory)
    assert not plan.operations
    assert any("group membership is insufficient" in reason for reason in plan.blockers)


def test_resealed_arbitrary_operation_does_not_bypass_reproducible_plan_check():
    spec, inventory = namespace_fixture()
    plan = make_plan(spec, inventory)
    altered = replace(plan.operations[1], resource="other/migration-namespace-read")
    plan = replace(plan, operations=[plan.operations[0], altered]).sealed()
    with pytest.raises(ConfigError, match="reproduced"):
        validate_plan(plan)


def test_inventory_rejects_secret_and_forged_absent_observation():
    _, inventory = namespace_fixture()
    raw = primitive(inventory)
    raw["source"]["trust"] = [{"client_secret": "must-not-echo"}]
    with pytest.raises(ConfigError) as error:
        Inventory.from_dict(raw)
    assert "must-not-echo" not in str(error.value)
    raw = primitive(inventory)
    key = next(iter(raw["target"]["observations"]))
    raw["target"]["observations"][key] = {"exists": False, "body": {"rules": []}, "version": None}
    with pytest.raises(ConfigError):
        Inventory.from_dict(raw)


def test_openstack_live_factory_pins_deployment_url_before_authentication(monkeypatch):
    openstack = pytest.importorskip('openstack')
    from migration.identity.adapters.openstack import from_environment
    from migration.identity.models import Endpoint
    endpoint=Endpoint('openstack','project-1','group-1','project-1',{'domain_id':'domain-1','role_id':'reader','url':'https://reviewed.example.invalid/v3'})
    monkeypatch.setenv('OS_AUTH_URL','https://other.example.invalid/v3')
    def unexpected(**kwargs):raise AssertionError('must block before authentication')
    monkeypatch.setattr(openstack,'connect',unexpected)
    with pytest.raises(BlockedError,match='URL differs'):
        from_environment(endpoint)
