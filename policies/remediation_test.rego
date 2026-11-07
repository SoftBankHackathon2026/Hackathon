# opa test . -v
package ghost.remediation_test

import data.ghost.remediation

now := "2026-11-04T09:00:00Z"

fresh := "2026-11-04T08:59:00Z"

stale := "2026-11-04T08:00:00Z"

base := {
	"schema_version": "1",
	"job_id": "forward_001",
	"stage": "remediation",
	"now": now,
	"plan": {
		"digest": "sha256:plan-1",
		"limits": {"evidence_ttl_seconds": 300},
		"remediation": {"auto_apply": true},
	},
	"scope": {
		"allowed_keys": ["DB_HOST", "DB_PORT"],
		"allowed_values": {"DB_HOST": ["migration-demo.rds.internal"], "DB_PORT": ["5432"]},
	},
	"proposal": {
		"digest": "sha256:prop-1",
		"changes": [{"key": "DB_HOST", "old": "wrong-host", "new": "migration-demo.rds.internal"}],
	},
	"evidence": {
		"snapshot_id": "snap-1",
		"checks": {"remediation_probe": probe_ok},
	},
}

probe_ok := {
	"status": "pass",
	"observed_at": fresh,
	"source": "agent.probe",
	"evidence_id": "ev-1",
	"detail": {"db_connected": true, "proposal_digest": "sha256:prop-1"},
}

with_probe(inp, p) := object.union(inp, {"evidence": {"snapshot_id": "snap-1", "checks": {"remediation_probe": p}}})

test_allow_when_in_scope_and_probe_passes if {
	d := remediation.decision with input as base
	d.outcome == "allow"
	d.allowed == true
	count(d.reason_codes) == 0
	d.proposal_digest == "sha256:prop-1"
}

test_deny_key_outside_scope if {
	inp := object.union(base, {"proposal": {"digest": "sha256:prop-2", "changes": [{"key": "DB_NAME", "old": "a", "new": "b"}]}})
	d := remediation.decision with input as inp
	d.outcome == "deny"
	"REM-01-KEY-NOT-ALLOWED" in d.reason_codes
}

test_deny_value_not_in_plan if {
	inp := object.union(base, {"proposal": {"digest": "sha256:prop-3", "changes": [{"key": "DB_HOST", "old": "x", "new": "attacker.example"}]}})
	d := remediation.decision with input as inp
	d.outcome == "deny"
	"REM-02-VALUE-NOT-APPROVED" in d.reason_codes
}

test_deny_secret_key_even_if_allowed if {
	inp := object.union(base, {
		"scope": {"allowed_keys": ["DB_PASSWORD"]},
		"proposal": {"digest": "sha256:prop-4", "changes": [{"key": "DB_PASSWORD", "old": "", "new": "x"}]},
	})
	d := remediation.decision with input as inp
	d.outcome == "deny"
	"REM-03-SECRET-KEY" in d.reason_codes
}

test_deny_empty_proposal if {
	inp := object.union(base, {"proposal": {"digest": "sha256:prop-5", "changes": []}})
	d := remediation.decision with input as inp
	d.outcome == "deny"
	"REM-00-EMPTY-PROPOSAL" in d.reason_codes
}

test_hold_when_probe_missing if {
	inp := json.patch(base, [{"op": "remove", "path": "/evidence/checks/remediation_probe"}])
	d := remediation.decision with input as inp
	d.outcome == "hold"
	"REM-04-PROBE-MISSING" in d.reason_codes
}

test_hold_when_probe_failed if {
	d := remediation.decision with input as with_probe(base, object.union(probe_ok, {"status": "fail"}))
	d.outcome == "hold"
	"REM-04-PROBE-FAILED" in d.reason_codes
}

test_hold_when_probe_says_not_connected if {
	d := remediation.decision with input as with_probe(base, object.union(probe_ok, {"detail": {"db_connected": false, "proposal_digest": "sha256:prop-1"}}))
	d.outcome == "hold"
	"REM-04-PROBE-FAILED" in d.reason_codes
}

test_hold_when_probe_stale if {
	d := remediation.decision with input as with_probe(base, object.union(probe_ok, {"observed_at": stale}))
	d.outcome == "hold"
	"REM-04-PROBE-STALE" in d.reason_codes
}

test_hold_when_probe_for_other_proposal if {
	d := remediation.decision with input as with_probe(base, object.union(probe_ok, {"detail": {"db_connected": true, "proposal_digest": "sha256:other"}}))
	d.outcome == "hold"
	"REM-04-PROBE-PROPOSAL-MISMATCH" in d.reason_codes
}

test_hold_when_probe_error if {
	d := remediation.decision with input as with_probe(base, object.union(probe_ok, {"status": "error"}))
	d.outcome == "hold"
	"REM-04-PROBE-UNVERIFIED" in d.reason_codes
}

test_hold_approval_required_when_auto_apply_off if {
	inp := object.union(base, {"plan": {"digest": "sha256:plan-1", "limits": {"evidence_ttl_seconds": 300}, "remediation": {"auto_apply": false}}})
	d := remediation.decision with input as inp
	d.outcome == "hold"
	"APPROVAL-REQUIRED" in d.reason_codes
}

test_allow_with_human_approval_when_auto_apply_off if {
	inp := object.union(base, {
		"plan": {"digest": "sha256:plan-1", "limits": {"evidence_ttl_seconds": 300}, "remediation": {"auto_apply": false}},
		"approval": {"approved": true, "approver": "gayeon", "proposal_digest": "sha256:prop-1"},
	})
	d := remediation.decision with input as inp
	d.outcome == "allow"
}

test_deny_wins_over_hold if {
	inp := json.patch(base, [
		{"op": "replace", "path": "/proposal", "value": {"digest": "sha256:prop-6", "changes": [{"key": "DB_NAME", "old": "a", "new": "b"}]}},
		{"op": "remove", "path": "/evidence/checks/remediation_probe"},
	])
	d := remediation.decision with input as inp
	d.outcome == "deny"
}

test_invalid_when_stage_wrong if {
	inp := object.union(base, {"stage": "pre_cutover"})
	d := remediation.decision with input as inp
	d.outcome == "invalid"
	"STAGE-UNKNOWN" in d.reason_codes
}

test_invalid_when_scope_missing if {
	inp := object.remove(base, ["scope"])
	d := remediation.decision with input as inp
	d.outcome == "invalid"
	"SCOPE-MISSING" in d.reason_codes
}

test_invalid_when_proposal_missing if {
	inp := object.remove(base, ["proposal"])
	d := remediation.decision with input as inp
	d.outcome == "invalid"
	"PROPOSAL-MISSING" in d.reason_codes
}
