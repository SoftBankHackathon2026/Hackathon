"""Tie review to reproducible plan, source evidence and current target state."""
import copy
from .models import BlockedError, ConfigError, Plan, digest, fields, string, ordered_evidence, state_digest
from .planner import make_plan


def validate_plan(plan: Plan) -> None:
    if plan.sealed().plan_digest != plan.plan_digest:
        raise ConfigError("plan contents changed after review")
    # A checksum alone cannot authorize arbitrary hand-edited operations.
    reconstructed = make_plan(plan.spec, plan.inventory)
    if reconstructed.plan_digest != plan.plan_digest:
        raise ConfigError("plan cannot be reproduced from its specification, inventory and installed rules")
    if plan.blockers:
        raise BlockedError("plan is blocked: " + "; ".join(plan.blockers))
    if not plan.operations:
        raise BlockedError("plan contains no supported operations")
    seen = set()
    for op in plan.operations:
        if op.id in seen or not set(op.depends_on) <= seen or op.expected is None:
            raise ConfigError("invalid operation ordering or missing expected state")
        if op.provider != plan.spec.target.provider or op.scope != plan.spec.target.scope or op.kind not in plan.spec.allowed_changes:
            raise BlockedError("operation is outside reviewed scope")
        seen.add(op.id)


def validate_review(plan: Plan, review: dict) -> None:
    fields(review, {"schema_version", "plan_digest", "reviewer", "reference"})
    if type(review["schema_version"]) is not int or review["schema_version"] != 1 or review["plan_digest"] != plan.plan_digest:
        raise BlockedError("review does not bind this exact plan")
    string(review["reviewer"], "reviewer")
    string(review["reference"], "review reference")


def evidence_digest(side: dict) -> str:
    # Collection wall-clock time and diagnostics are not policy state.
    return digest(ordered_evidence({key: value for key, value in side.items() if key not in {"collected_at", "evidence", "limitations", "observations"}}))


def refresh_source(plan: Plan, source_adapter) -> None:
    fresh = source_adapter.collect()
    if evidence_digest(fresh) != evidence_digest(plan.inventory.source):
        raise BlockedError("source identity, policy or coverage changed; recollect and review a new plan")


def target_context(plan: Plan, side: dict) -> str:
    """Remove only the exact managed deltas; retain all other policy context.

    This is not a policy-equivalence evaluator. Any representation we cannot
    account for remains a difference and stops resume for manual review.
    """
    side = copy.deepcopy(side)
    operations = plan.operations
    if not plan.inventory.target["identities"]:
        # Created identity UIDs are separately pinned in durable operation after states.
        side["identities"] = []
        side["trust"] = [t for t in side["trust"] if not (
            (t.get("kind") == "gcp.service_account_policy" and not t.get("policy", {}).get("bindings")
             and set(t.get("policy", {})) <= {"bindings", "etag", "version"}) or
            any(op.kind == "aws.role" and digest(t) == digest(op.desired["trust_policy"]) for op in operations))]
    policies = []
    for item in side["policies"]:
        if any(op.kind == "gcp.custom_role" and item.get("kind") == "gcp.role" and item.get("name") == op.resource
               and item.get("included_permissions") == op.desired["included_permissions"] for op in operations):
            continue
        if any(op.kind == "aws.inline_policy" and digest(item) == digest(op.desired["policy"]) for op in operations):
            continue
        if item.get("kind") == "gcp.allow_policy" and item.get("resource") == plan.spec.target.resource:
            policy = item["policy"]
            policy.pop("etag", None)
            bindings = []
            for binding in policy.get("bindings", []):
                for op in operations:
                    if op.kind == "gcp.bucket_binding" and set(binding) == {"role", "members"} and binding["role"] == op.desired["role"]:
                        binding["members"] = [m for m in binding["members"] if m != op.desired["member"]]
                if binding.get("members"):
                    bindings.append(binding)
            policy["bindings"] = sorted(bindings, key=digest)
        if item.get("kind") == "kubernetes.role" and any(op.kind == "kubernetes.role" and
                item.get("metadata", {}).get("name") == op.resource.split("/")[1] and
                item.get("rules") == op.desired["rules"] for op in operations):
            continue
        policies.append(item)
    side["policies"] = sorted(policies, key=digest)
    # Other relationships remain protected; known RoleBindings are checked by observe.
    side["relationships"] = [r for r in side.get("relationships", []) if not any(
        op.kind == "kubernetes.role_binding" and r.get("metadata", {}).get("name") == op.resource.split("/")[1]
        and r.get("roleRef") == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": op.desired["role_name"]}
        and r.get("subjects") == [{"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": op.desired["group"], "namespace": None}]
        for op in operations)]
    return evidence_digest(side)


def refresh_target(plan: Plan, target_adapter) -> None:
    fresh = target_adapter.collect()
    if target_context(plan, fresh) != target_context(plan, plan.inventory.target):
        raise BlockedError("target policy context, identity or coverage changed outside managed deltas")


def preflight(plan: Plan, source_adapter=None, target_adapter=None) -> dict:
    validate_plan(plan)
    if source_adapter is None or target_adapter is None:
        return {"status": "offline_valid", "live_checked": False,
                "limitations": ["source authentication and target policy state have not been checked"]}
    refresh_source(plan, source_adapter)
    refresh_target(plan, target_adapter)
    for op in plan.operations:
        if state_digest(target_adapter.observe(op)) != state_digest(op.expected):
            raise BlockedError("target state changed since collection")
    return {"status": "ready", "live_checked": True}
