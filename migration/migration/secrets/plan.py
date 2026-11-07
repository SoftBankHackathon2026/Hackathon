"""Versioned policy evidence and stable source/subject/path bindings.

Consumes PR #51 artifacts without importing or modifying that unmerged module.
No policy is applied by this module; future operations are data, not ACL grants.
"""

from __future__ import annotations

from .common import (
    check_artifact,
    digest,
    https_url,
    keys,
    load,
    logical_path,
    now,
    require,
    seal_artifact,
    source_fingerprint,
    string,
)

OPERATIONS = ("read", "create", "update", "delete", "restore", "destroy", "manage")


def preparation(config):
    configured = load(config["preparation"]["configure_result"])
    verified = load(config["preparation"]["verify_result"])
    require(
        configured.get("schema_version") == 1
        and configured.get("operation") == "configure"
        and configured.get("status") in {"configured", "partially_ready"}
        and not configured.get("secret_transfer_performed"),
        "OPENBAO_CONFIGURE_RESULT_REQUIRED",
    )
    require(
        verified.get("schema_version") == 1
        and verified.get("operation") == "verify"
        and verified.get("status") in {"ready", "partially_ready"},
        "OPENBAO_VERIFY_RESULT_REQUIRED",
    )
    reports = [r for r in configured.get("reports", []) if "kv_mount" in r]
    require(
        len(reports) == 1
        and reports[0]["kv_mount"] == config["target"]["mount"]
        and reports[0]["path_prefix"] == config["target"]["prefix"],
        "PREPARATION_SCOPE_MISMATCH",
    )
    checks = [r["checks"] for r in verified.get("reports", []) if "checks" in r]
    require(
        len(checks) == 1
        and all(checks[0].get(k) == "passed" for k in ("service", "migration")),
        "PREPARATION_SERVICE_OR_MIGRATION_UNVERIFIED",
    )
    require(
        configured.get("request_id") == verified.get("request_id"),
        "PREPARATION_REQUEST_MISMATCH",
    )
    return {
        "configure_hash": digest(configured),
        "verify_hash": digest(verified),
        "checks": checks[0],
        "infrastructure_status": verified["status"],
    }


def identity_context(config):
    if not config.get("identity"):
        return (
            {},
            {
                "inventory_hash": None,
                "collected_at": None,
                "source": {},
                "policies": [],
                "coverage": [
                    {"scope": "identity_policy_and_subjects", "status": "not_collected"}
                ],
            },
            {"plan_hash": None, "mapping_rows": [], "resources": []},
        )
    c, inv, plan = (
        load(config["identity"][k]) for k in ("config", "inventory", "plan")
    )
    check_artifact(inv, "identity_inventory", "inventory_hash")
    check_artifact(plan, "identity_plan", "plan_hash")
    require(
        plan.get("status") == "planned" and not plan.get("blockers"),
        "IDENTITY_PLAN_BLOCKED",
    )
    require(
        plan.get("inventory_hash") == inv["inventory_hash"]
        and plan.get("config_hash") == digest(c),
        "IDENTITY_INPUT_CHANGED",
    )
    b = c.get("targets", {}).get("openbao", {})
    require(
        https_url(b.get("base_url")) == https_url(config["target"]["endpoint"]),
        "IDENTITY_ENDPOINT_MISMATCH",
    )
    require(
        config["source"]["account_id"] in inv["source"]["account_ids"],
        "IDENTITY_SOURCE_ACCOUNT_MISMATCH",
    )
    return c, inv, plan


def policy_limitations(document):
    if not document:
        return []
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    require(isinstance(statements, list), "POLICY_DOCUMENT_INVALID")
    reasons = set()
    for st in statements:
        require(isinstance(st, dict), "POLICY_DOCUMENT_INVALID")
        if st.get("Effect") != "Allow" or any(
            k in st for k in ("Condition", "NotAction", "NotResource", "NotPrincipal")
        ):
            reasons.add("SOURCE_RESOURCE_POLICY_RESTRICTION_UNSUPPORTED")
    return sorted(reasons)


def build_plan(config, inventory, mapping):
    check_artifact(inventory, "secret_inventory", "inventory_hash")
    require(
        inventory["source"]
        == {k: config["source"][k] for k in ("account_id", "region")},
        "INVENTORY_SOURCE_MISMATCH",
    )
    require(
        inventory["source_fingerprint"] == source_fingerprint(inventory),
        "INVENTORY_FINGERPRINT_MISMATCH",
    )
    keys(
        mapping,
        {"schema_version", "inventory_hash", "bindings"},
        {"schema_version", "inventory_hash", "bindings"},
    )
    require(
        type(mapping["schema_version"]) is int
        and mapping["schema_version"] == 1
        and mapping["inventory_hash"] == inventory["inventory_hash"],
        "STALE_MAPPING",
    )
    require(
        isinstance(mapping["bindings"], list) and mapping["bindings"],
        "MAPPING_REQUIRED",
    )
    ic, ii, ip = identity_context(config)
    ready = preparation(config)
    resources = {r["arn"]: r for r in inventory["resources"]}
    require(
        len(resources) == len(inventory["resources"]) and resources,
        "SOURCE_SELECTION_EMPTY_OR_DUPLICATE",
    )
    plan = {
        "schema_version": 1,
        "kind": "secret_plan",
        "migration_id": config["migration_id"],
        "config_hash": digest(config),
        "inventory_hash": inventory["inventory_hash"],
        "source_fingerprint": inventory["source_fingerprint"],
        "source": inventory["source"],
        "target": {
            k: config["target"][k]
            for k in ("endpoint", "cluster_id", "mount", "prefix", "expected_version")
        },
        "identity_config_hash": digest(ic),
        "identity_inventory_hash": ii["inventory_hash"],
        "identity_plan_hash": ip["plan_hash"],
        "identity_status": "linked" if ip["plan_hash"] else "deferred_not_verified",
        "preparation": ready,
        "rows": [],
        "policy_evidence": [],
        "blockers": [],
        "limitations": inventory["limitations"],
        "application_status": "external_handoff_required",
        "rotation_status": "separate_explicit_command",
    }
    seen, paths = set(), set()
    identity_rows = {r["assignment_id"]: r for r in ip["mapping_rows"]}
    plan["blockers"].extend(
        {"code": "SOURCE_COLLECTION_INCOMPLETE", "scope": r["scope"]}
        for r in inventory.get("coverage", [])
        if r.get("status") != "collected"
    )
    for b in mapping["bindings"]:
        keys(
            b,
            {
                "source_arn",
                "target_path",
                "business",
                "environment",
                "identity_assignments",
                "review_ref",
                "access_mode",
            },
            {
                "source_arn",
                "target_path",
                "business",
                "environment",
                "identity_assignments",
                "review_ref",
            },
        )
        arn, path = b["source_arn"], logical_path(b["target_path"])
        require(
            arn in resources and arn not in seen and path not in paths,
            "DUPLICATE_OR_UNKNOWN_BINDING",
        )
        require(
            path.startswith(config["target"]["prefix"] + "/"), "TARGET_SCOPE_MISMATCH"
        )
        seen.add(arn)
        paths.add(path)
        for field in ("business", "environment", "review_ref"):
            string(b[field])
        assignments = b["identity_assignments"]
        mode = b.get("access_mode", "identity")
        require(mode in {"identity", "deferred"}, "ACCESS_MODE_INVALID")
        require(
            isinstance(assignments, list)
            and all(isinstance(a, str) for a in assignments)
            and len(set(assignments)) == len(assignments),
            "IDENTITY_ASSIGNMENTS_REQUIRED",
        )
        if mode == "identity":
            require(assignments and ip["plan_hash"], "IDENTITY_ASSIGNMENTS_REQUIRED")
        else:
            require(not assignments, "DEFERRED_ACCESS_MUST_NOT_CLAIM_ASSIGNMENTS")
        subjects, docs = [], []
        full_path = config["target"]["mount"] + "/data/" + path
        for aid in assignments:
            require(aid in identity_rows, "IDENTITY_ASSIGNMENT_UNKNOWN")
            row = identity_rows[aid]
            src = row["source"]
            require(
                src["account_id"] == config["source"]["account_id"]
                and row["business"] == b["business"]
                and row["environment"] == b["environment"],
                "IDENTITY_SCOPE_MISMATCH",
            )
            require(
                {"path": full_path, "capabilities": ["read"]}
                in row["targets"].get("openbao", []),
                "IDENTITY_READ_PATH_NOT_MAPPED",
            )
            require(
                any(
                    r["kind"] == "bao_policy"
                    and {"path": full_path, "capabilities": ["read"]}
                    in r["payload"].get("paths", [])
                    for r in ip["resources"]
                ),
                "IDENTITY_POLICY_MISSING",
            )
            subjects.append(
                {
                    "account_id": src["account_id"],
                    "identity_store_id": ii["source"]["identity_store_id"],
                    "principal_type": src["principal_type"],
                    "principal_id": src["principal_id"],
                    "permission_set_arn": src["permission_set_arn"],
                    "target_group": row["target_group"],
                    "assignment_id": aid,
                    "review_ref": row["review_ref"],
                    "source_access_evidence": row["source_access_evidence"],
                }
            )
            relevant = [
                p
                for p in ii["policies"]
                if p["account_id"] == src["account_id"]
                and p["permission_set_arn"] == src["permission_set_arn"]
            ]
            require(
                len(relevant) == 1 and relevant[0].get("documents"),
                "IDENTITY_POLICY_EVIDENCE_MISSING",
            )
            docs.append(relevant[0])
        resource = resources[arn]
        blockers = policy_limitations(resource["resource_policy"])
        if policy_limitations(resource["kms"].get("policy")):
            blockers.append("SOURCE_KMS_POLICY_RESTRICTION_UNSUPPORTED")
        if resource["kms"]["coverage"] == "unknown":
            blockers.append("KMS_POLICY_EVIDENCE_MISSING")
        if resource["scheduled_deletion"] or resource["managed_by_service"]:
            blockers.append("SOURCE_LIFECYCLE_OR_SERVICE_MANAGED")
        selected = {}
        for label in ("AWSCURRENT", "AWSPREVIOUS"):
            matches = [v["id"] for v in resource["versions"] if label in v["stages"]]
            require(
                len(matches) <= 1 and (matches or label == "AWSPREVIOUS"),
                "SOURCE_CURRENT_OR_PREVIOUS_AMBIGUOUS",
            )
            if matches:
                selected[label] = matches[0]
        if any("AWSPENDING" in v["stages"] for v in resource["versions"]):
            blockers.append("SOURCE_ROTATION_PENDING")
        binding = {
            "access_mode": mode,
            "source": {**inventory["source"], "arn": arn},
            "target": {
                "cluster_id": config["target"]["cluster_id"],
                "mount": config["target"]["mount"],
                "path": path,
            },
            "business": b["business"],
            "environment": b["environment"],
            "subjects": subjects,
        }
        evidence = {
            "source": binding["source"],
            "collected_at": inventory["collected_at"],
            "identity_collected_at": ii["collected_at"],
            "resource_policy": resource["resource_policy"],
            "kms": resource["kms"],
            "identity_policy_documents": docs,
            "identity_coverage": ii["coverage"],
            "review_ref": b["review_ref"],
            "source_description": resource["description"],
            "source_tags": resource["tags"],
            "interpretation": "reviewed_mapping_not_effective_AWS_policy_equivalence",
            "limitations": blockers,
            "access_limitations": ["IDENTITY_MAPPING_DEFERRED"]
            if mode == "deferred"
            else [],
        }
        plan["policy_evidence"].append(evidence)
        plan["rows"].append(
            {
                "binding_id": digest(binding),
                **binding,
                "versions": selected,
                "previous_status": "selected"
                if "AWSPREVIOUS" in selected
                else "not_present",
                "omitted_versions": sorted(
                    v["id"]
                    for v in resource["versions"]
                    if v["id"] not in set(selected.values())
                ),
                "rotation_enabled_at_source": resource["rotation_enabled"],
                "review_ref": b["review_ref"],
                "operations": {
                    op: {
                        "automatic_grant": False,
                        "status": "linked_via_identity_plan"
                        if op == "read" and mode == "identity"
                        else "deferred_not_implemented",
                    }
                    for op in OPERATIONS
                },
            }
        )
        plan["blockers"].extend(
            {"source_arn": arn, "code": reason} for reason in blockers
        )
    require(seen == set(resources), "UNMAPPED_SELECTED_SECRET")
    plan["rows"].sort(key=lambda r: r["source"]["arn"])
    plan["status"] = "blocked" if plan["blockers"] else "planned"
    return seal_artifact(plan, "plan_hash")


def validate_plan(config, plan):
    check_artifact(plan, "secret_plan", "plan_hash")
    require(
        plan.get("status") == "planned" and not plan["blockers"] and plan["rows"],
        "PLAN_BLOCKED",
    )
    require(plan["config_hash"] == digest(config), "CONFIG_CHANGED")
    ic, ii, ip = identity_context(config)
    require(
        (digest(ic), ii["inventory_hash"], ip["plan_hash"])
        == (
            plan["identity_config_hash"],
            plan["identity_inventory_hash"],
            plan["identity_plan_hash"],
        ),
        "IDENTITY_INPUT_CHANGED",
    )
    require(preparation(config) == plan["preparation"], "PREPARATION_CHANGED")


def preflight(config, plan, source, target):
    validate_plan(config, plan)
    fresh = source.inventory()
    require(
        fresh["source_fingerprint"] == plan["source_fingerprint"],
        "SOURCE_CHANGED_REPLAN_REQUIRED",
    )
    observed = target.inspect([r["target"]["path"] for r in plan["rows"]])
    conflicts = []
    for row in plan["rows"]:
        existing = target.read_document(row["target"]["path"])
        # Same plan retries are reconciled at apply; never adopt existing data by value alone.
        if existing is not None and (
            existing[0].get("owner") != row["binding_id"]
            or existing[0].get("copy_operation")
            != digest([plan["plan_hash"], row["binding_id"], "copy"])
        ):
            conflicts.append(row["binding_id"])
    result = {
        "schema_version": 1,
        "kind": "secret_preflight",
        "plan_hash": plan["plan_hash"],
        "checked_at": now(),
        "target": observed,
        "conflicts": conflicts,
        "status": "blocked" if conflicts else "ready",
        "write_execution_verified": False,
    }
    return seal_artifact(result, "preflight_hash")
