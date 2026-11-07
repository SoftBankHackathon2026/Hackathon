from __future__ import annotations

import copy

import pytest
from migration.identity.common import (
    IdentityError,
    digest,
    load,
    private_write,
    validate_config,
)
from migration.identity.plan import (
    create_plan,
    mapping_template,
    policy_blockers,
    validate_paths,
    validate_rules,
)


def rehash(inv):
    inv.pop("inventory_hash")
    inv["inventory_hash"] = digest(inv)


def test_account_scoped_groups_and_direct_assignment(
    identity_config, identity_inventory, identity_mapping
):
    inv, mapping = copy.deepcopy(identity_inventory), copy.deepcopy(identity_mapping)
    a = copy.deepcopy(inv["assignments"][0])
    a.update(principal_type="USER", principal_id="u-1")
    inv["assignments"].append(a)
    rehash(inv)
    generated = mapping_template(inv)["bindings"][1]
    generated.update(
        {
            k: mapping["bindings"][0][k]
            for k in (
                "business",
                "environment",
                "targets",
                "review_ref",
                "source_access_evidence",
            )
        }
    )
    mapping["bindings"].append(generated)
    mapping["inventory_hash"] = inv["inventory_hash"]
    plan = create_plan(identity_config, inv, mapping)
    assert not plan["blockers"]
    grants = [
        r
        for r in plan["resources"]
        if r["kind"] == "kc_group" and "identity.account" in r["payload"]["attributes"]
    ]
    assert len(grants) == 1
    assert len({r["id"] for r in plan["resources"]}) == len(plan["resources"])


def test_deterministic_realm_and_handoffs(
    identity_config, identity_inventory, identity_mapping
):
    a = create_plan(identity_config, identity_inventory, identity_mapping)
    b = create_plan(identity_config, identity_inventory, identity_mapping)
    assert a == b and a["status"] == "planned"
    assert {r["kind"] for r in a["resources"]} >= {
        "kc_realm",
        "bao_role",
        "k8s_binding",
        "kc_group_role",
    }
    assert {h["kind"] for h in a["handoffs"]} == {
        "credential_enrollment",
        "kubernetes_authentication",
        "application_configuration",
    }
    assert "client_secret" not in str(a)


@pytest.mark.parametrize(
    "statement,reason",
    [
        (
            {"Effect": "Deny", "Action": "s3:GetObject", "Resource": "*"},
            "EXPLICIT_DENY_OR_INVALID_EFFECT",
        ),
        (
            {"Effect": "Allow", "NotAction": "iam:*", "Resource": "*"},
            "UNSUPPORTED_POLICY_SEMANTICS",
        ),
        (
            {
                "Effect": "Allow",
                "Action": "s3:GetObject",
                "Resource": "*",
                "Condition": {},
            },
            "UNSUPPORTED_POLICY_SEMANTICS",
        ),
        (
            {"Effect": "Allow", "Action": "s3:*", "Resource": "*"},
            "WILDCARD_ACTION_REQUIRES_FUTURE_TRANSLATOR",
        ),
        ({"Effect": "Allow", "Action": []}, "INVALID_ACTION"),
    ],
)
def test_unsupported_policy_never_silently_translates(statement, reason):
    assert reason in policy_blockers({"Statement": statement})


def test_partial_coverage_and_unreviewed_mapping_block(
    identity_config, identity_inventory
):
    identity_inventory["coverage"].append({"scope": "policies", "status": "unknown"})
    rehash(identity_inventory)
    plan = create_plan(
        identity_config, identity_inventory, mapping_template(identity_inventory)
    )
    codes = {b["code"] for b in plan["blockers"]}
    assert {
        "SOURCE_COVERAGE_INCOMPLETE",
        "MAPPING_REVIEW_REQUIRED",
        "TARGET_MAPPING_MISSING",
    } <= codes


def test_stale_inventory_and_mapping(
    identity_config, identity_inventory, identity_mapping
):
    identity_inventory["users"][0]["username"] = "changed"
    with pytest.raises(IdentityError, match="INVENTORY_HASH_MISMATCH"):
        create_plan(identity_config, identity_inventory, identity_mapping)
    rehash(identity_inventory)
    with pytest.raises(IdentityError, match="STALE_MAPPING"):
        create_plan(identity_config, identity_inventory, identity_mapping)


def test_unmapped_and_explicitly_excluded_assignments(
    identity_config, identity_inventory, identity_mapping
):
    aid = identity_mapping["bindings"][0]["assignment_id"]
    identity_mapping["bindings"] = []
    assert (
        create_plan(identity_config, identity_inventory, identity_mapping)["blockers"][
            0
        ]["code"]
        == "UNMAPPED_ASSIGNMENT"
    )
    identity_mapping["excluded_assignments"] = [
        {"assignment_id": aid, "reason": "Storage integration deferred by owner"}
    ]
    assert (
        create_plan(identity_config, identity_inventory, identity_mapping)[
            "exclusions"
        ][0]["assignment_id"]
        == aid
    )


def test_account_collapse_is_rejected(
    identity_config, identity_inventory, identity_mapping
):
    import copy

    second = "444455556666"
    identity_config["source"]["account_ids"].append(second)
    identity_inventory["source"]["account_ids"].append(second)
    a = copy.deepcopy(identity_inventory["assignments"][0])
    a["account_id"] = second
    identity_inventory["assignments"].append(a)
    policy = copy.deepcopy(identity_inventory["policies"][0])
    policy["account_id"] = second
    identity_inventory["policies"].append(policy)
    rehash(identity_inventory)
    other = mapping_template(identity_inventory)["bindings"][1]
    other.update(
        {
            k: identity_mapping["bindings"][0][k]
            for k in (
                "business",
                "environment",
                "targets",
                "review_ref",
                "source_access_evidence",
            )
        }
    )
    identity_mapping["bindings"].append(other)
    identity_mapping["inventory_hash"] = identity_inventory["inventory_hash"]
    with pytest.raises(IdentityError, match="ACCOUNT_OR_ENVIRONMENT_COLLAPSE"):
        create_plan(identity_config, identity_inventory, identity_mapping)


@pytest.mark.parametrize(
    "path", ["sys/data/root", "kv/data/../x", "kv/data/a*", "kv/data//x"]
)
def test_unsafe_bao_paths(path):
    with pytest.raises(IdentityError):
        validate_paths([{"path": path, "capabilities": ["read"]}])


@pytest.mark.parametrize(
    "resource,verb",
    [("secrets", "get"), ("pods/exec", "get"), ("pods", "delete"), ("*", "get")],
)
def test_privileged_kubernetes_rules_rejected(resource, verb):
    with pytest.raises(IdentityError):
        validate_rules([{"apiGroups": [""], "resources": [resource], "verbs": [verb]}])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.update(schema_version=True),
        lambda c: c["targets"]["keycloak"].update(realm="master"),
        lambda c: c["targets"]["keycloak"].update(base_url="http://unsafe.test"),
        lambda c: c["targets"]["keycloak"].update(admin_password="never-serialize"),
        lambda c: c["targets"]["applications"][0].update(
            redirect_uris=["https://app.test/*"]
        ),
    ],
)
def test_strict_config(identity_config, mutation):
    mutation(identity_config)
    with pytest.raises(IdentityError):
        validate_config(identity_config)


def test_duplicate_yaml_keys_and_private_artifacts(tmp_path):
    file = tmp_path / "bad.yml"
    file.write_text("schema_version: 1\nschema_version: 2\n")
    with pytest.raises(IdentityError, match="DUPLICATE_OR_INVALID_KEY"):
        load(file)
    private_write(file, {"hello": "world"})
    assert file.stat().st_mode & 0o777 == 0o600


def rehash_plan(plan):
    plan["plan_hash"] = digest({k: v for k, v in plan.items() if k != "plan_hash"})
    return plan


@pytest.mark.parametrize(
    ("kind", "change", "code"),
    [
        (
            "k8s_role",
            lambda p: p["rules"][0].update(verbs=["*"]),
            "KUBERNETES_READ_ONLY_INITIAL_SCOPE",
        ),
        ("k8s_role", lambda p: p.update(kind="ClusterRole"), "KUBERNETES_ROLE_ONLY"),
        (
            "bao_policy",
            lambda p: p["paths"][0].update(path="kv/*"),
            "EXACT_KV2_DATA_PATH_REQUIRED",
        ),
    ],
)
def test_validate_plan_rechecks_grants_after_edits(
    identity_config, identity_plan, kind, change, code
):
    from migration.identity.plan import validate_plan

    plan = copy.deepcopy(identity_plan)
    target = next(r for r in plan["resources"] if r["kind"] == kind)
    change(target["payload"])
    with pytest.raises(IdentityError, match=code):
        validate_plan(rehash_plan(plan), identity_config)


def test_validate_plan_rejects_unknown_kinds(identity_config, identity_plan):
    from migration.identity.plan import validate_plan

    plan = copy.deepcopy(identity_plan)
    plan["resources"][0]["kind"] = "k8s_cluster_role_binding"
    with pytest.raises(IdentityError, match="UNSUPPORTED_RESOURCE_KIND"):
        validate_plan(rehash_plan(plan), identity_config)
    assert validate_plan(identity_plan, identity_config) == identity_plan
