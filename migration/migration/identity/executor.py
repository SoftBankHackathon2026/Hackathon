"""Journal-before-write execution and bounded readback after ambiguous responses."""
from __future__ import annotations
from .models import BlockedError, ConflictError, IdentityError, Plan, UnknownOutcome, state_digest as digest, primitive
from .preflight import refresh_source, refresh_target, validate_plan, validate_review
from .state import State

SUCCESS = {"applied", "reused", "reconciled"}


def execute(plan: Plan, review: dict, state: State, source_adapter, target_adapter) -> dict:
    validate_plan(plan)
    validate_review(plan, review)
    with state.lock():
        state.bind(plan.plan_digest, review)
        refresh_source(plan, source_adapter)
        refresh_target(plan, target_adapter)
        # Check all untouched operations before making the first change.
        for op in plan.operations:
            if state.get(plan.plan_digest, op.id) is None and digest(target_adapter.observe(op)) != digest(op.expected):
                raise ConflictError("target state changed before execution")
        for op in plan.operations:
            refresh_source(plan, source_adapter)
            refresh_target(plan, target_adapter)
            # Revalidate immutable identities and every completed prerequisite.
            for prior in plan.operations:
                if prior.id == op.id:
                    break
                completed = state.get(plan.plan_digest, prior.id)
                if completed and completed["status"] in SUCCESS:
                    if digest(target_adapter.observe(prior)) != digest(completed["after"]):
                        raise ConflictError("a completed resource or execution identity changed")
            record = state.get(plan.plan_digest, op.id)
            if record and record["status"] in SUCCESS:
                if digest(target_adapter.observe(op)) != digest(record["after"]):
                    raise ConflictError("previously completed operation has drifted")
                continue
            before = record["before"] if record else target_adapter.observe(op)
            current = target_adapter.observe(op)
            if record and record["status"] in {"started", "unknown"} and target_adapter.satisfied(op, current):
                state.record(plan.plan_digest, op.id, {"status": "reconciled", "before": before, "after": current,
                             "created": False, "ownership": "uncertain", "reason": "recovered_after_interruption"})
                continue
            if digest(current) != digest(before) or digest(before) != digest(op.expected):
                raise ConflictError("operation state changed; automatic retry would be unsafe")
            if target_adapter.satisfied(op, before):
                state.record(plan.plan_digest, op.id, {"status": "reused", "before": before, "after": before,
                             "created": False, "ownership": "preexisting"})
                continue
            state.record(plan.plan_digest, op.id, {"status": "started", "before": before, "created": False,
                         "ownership": "pending"})
            try:
                target_adapter.apply(op, before)
            except (ConflictError, BlockedError):
                state.record(plan.plan_digest, op.id, {"status": "conflict", "before": before,
                             "created": False, "ownership": "uncertain", "reason": "provider_rejected_or_conflict"})
                raise
            except Exception:
                # Provider exceptions may include tokens/HTTP bodies; never store them.
                for _ in range(3):
                    try:
                        current = target_adapter.observe(op)
                    except Exception:
                        continue
                    if target_adapter.satisfied(op, current):
                        state.record(plan.plan_digest, op.id, {"status": "reconciled", "before": before, "after": current,
                                     "created": False, "ownership": "uncertain", "reason": "response_lost_readback_matches"})
                        break
                else:
                    state.record(plan.plan_digest, op.id, {"status": "unknown", "before": before,
                                 "created": False, "ownership": "uncertain", "reason": "request_outcome_unconfirmed"})
                    raise UnknownOutcome("request outcome is unknown; resume will read state before retrying") from None
                continue
            try:
                after = target_adapter.observe(op)
            except Exception:
                state.record(plan.plan_digest, op.id, {"status": "unknown", "before": before,
                             "created": False, "ownership": "uncertain", "reason": "post_write_read_failed"})
                raise UnknownOutcome("post-write state could not be confirmed") from None
            if not target_adapter.satisfied(op, after):
                state.record(plan.plan_digest, op.id, {"status": "unknown", "before": before, "after": after,
                             "created": False, "ownership": "uncertain", "reason": "post_write_mismatch"})
                raise UnknownOutcome("post-write state does not yet match the plan")
            state.record(plan.plan_digest, op.id, {"status": "applied", "before": before, "after": after,
                         "created": not before["exists"], "ownership": "recorded_change"})
        return {"status": "applied_configuration", "access_verified": False, "operations": state.results(plan.plan_digest)}


def rollback_plan(plan: Plan, state: State) -> dict:
    """Read-only recovery proposals. Never delete a whole shared policy."""
    items = []
    for op in reversed(plan.operations):
        result = state.get(plan.plan_digest, op.id)
        if not result or result["status"] == "reused":
            continue
        if result.get("ownership") != "recorded_change" or result["status"] != "applied":
            action = "manual_reconciliation_required"
        elif op.kind.endswith(("bucket_binding", "role_binding", "role_assignment")):
            action = "remove_only_recorded_binding_delta_after_reference_and_version_check"
        elif result.get("created"):
            action = "consider_removing_created_resource_after_dependency_and_version_check"
        else:
            action = "manual_reconciliation_required"
        items.append({"operation_id": op.id, "provider": op.provider, "resource": op.resource, "action": action,
                      "added": primitive(op.desired), "before": result["before"], "expected_current": result.get("after"),
                      "automatic_apply_supported": False})
    return {"plan_digest": plan.plan_digest, "mode": "read_only_recovery_proposal", "items": items,
            "limitations": ["No automatic rollback execution; preserve unrelated and preexisting access", "Reconcile ambiguous ownership manually"]}
