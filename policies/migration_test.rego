# opa test . -v
package ghost.migration_test

import data.ghost.migration

now := "2026-11-03T12:00:00Z"

fresh := "2026-11-03T11:58:00Z"

stale := "2026-11-03T11:00:00Z"

check(status, detail) := {
	"status": status,
	"observed_at": fresh,
	"source": "evidence.test",
	"evidence_id": "ev-test-1",
	"detail": detail,
}

plan := {
	"digest": "sha256:plan-1",
	"source": "onprem",
	"target": "aws",
	"approved_locations": ["ap-northeast-2"],
	"limits": {"evidence_ttl_seconds": 300, "write_wait_max_seconds": 30},
}

all_checks := {
	"target_db_exposure": check("pass", {"public": false}),
	"target_location": check("pass", {"location": "ap-northeast-2"}),
	"target_app_db": check("pass", {"db_connected": true, "order_query_ok": true}),
	"final_sync": check("pass", {"source_writes_fenced": true, "target_applied_through_checkpoint": true, "compare_passed": true}),
	"write_wait": check("pass", {"elapsed_seconds": 12}),
}

base(stage) := {
	"schema_version": "1",
	"job_id": "forward_001",
	"stage": stage,
	"now": now,
	"plan": plan,
	"evidence": {"snapshot_id": "snap-1", "plan_digest": "sha256:plan-1", "checks": all_checks},
}

# object.union 은 재귀 병합이라 키를 지우거나 통째로 바꿀 수 없다. 검사 하나를 교체/삭제하는 헬퍼.
set_checks(inp, checks) := object.union(
	object.remove(inp, ["evidence"]),
	{"evidence": object.union(object.remove(inp.evidence, ["checks"]), {"checks": checks})},
)

with_check(inp, name, c) := set_checks(inp, object.union(object.remove(inp.evidence.checks, [name]), {name: c}))

without_check(inp, name) := set_checks(inp, object.remove(inp.evidence.checks, [name]))

with_detail(inp, name, detail) := with_check(inp, name, check("pass", detail))

# ---- allow ---------------------------------------------------------------

test_plan_allow if {
	d := migration.decision with input as base("plan")
	d.outcome == "allow"
	d.allowed == true
	d.evaluated_rules == ["SEC-02"]
	d.required_checks == ["target_location"]
}

test_pre_start_allow if {
	d := migration.decision with input as base("pre_start")
	d.allowed == true
	d.evaluated_rules == ["SEC-01", "SEC-02"]
}

test_pre_cutover_allow if {
	d := migration.decision with input as base("pre_cutover")
	d.allowed == true
	d.evaluated_rules == ["APP-01", "SEC-01", "SEC-02"]
	count(d.violations) == 0
}

test_final_verify_allow if {
	d := migration.decision with input as base("final_verify")
	d.allowed == true
	d.evaluated_rules == ["APP-01", "OPS-01", "OPS-02", "SEC-01"]
	d.policy_version == "migration-policy-v1"
	d.plan_digest == "sha256:plan-1"
	d.evidence_snapshot_id == "snap-1"
}

# 단계에 안 쓰는 검사는 없어도 된다.
test_plan_allow_without_other_checks if {
	inp := set_checks(base("plan"), {"target_location": all_checks.target_location})
	d := migration.decision with input as inp
	d.allowed == true
}

# ---- deny ----------------------------------------------------------------

test_public_db_deny if {
	d := migration.decision with input as with_detail(base("pre_start"), "target_db_exposure", {"public": true})
	d.outcome == "deny"
	d.allowed == false
	d.reason_codes == ["SEC-01-PUBLIC-DB"]
}

test_check_failed_deny if {
	d := migration.decision with input as with_check(base("pre_start"), "target_db_exposure", check("fail", {"public": true}))
	d.outcome == "deny"
	d.reason_codes == ["SEC-01-CHECK-FAILED"]
}

test_unapproved_location_deny if {
	d := migration.decision with input as with_detail(base("plan"), "target_location", {"location": "us-east-1"})
	d.outcome == "deny"
	d.reason_codes == ["SEC-02-UNAPPROVED-LOCATION"]
}

# C05 시나리오: 대상 앱 C 의 DB 연결 주소가 잘못됨 → 전환 보류(거절), B 는 계속 운영.
test_target_app_db_unreachable_deny if {
	d := migration.decision with input as with_detail(base("pre_cutover"), "target_app_db", {"db_connected": false, "order_query_ok": false})
	d.outcome == "deny"
	d.reason_codes == ["APP-01-ORDER-CHECK-FAILED", "APP-01-TARGET-DB-UNREACHABLE"]
}

# healthz 만 통과하고 주문 조회가 안 되면 통과가 아니다.
test_order_check_failed_deny if {
	d := migration.decision with input as with_detail(base("pre_cutover"), "target_app_db", {"db_connected": true, "order_query_ok": false})
	d.outcome == "deny"
	d.reason_codes == ["APP-01-ORDER-CHECK-FAILED"]
}

test_wait_limit_exceeded_deny if {
	d := migration.decision with input as with_detail(base("final_verify"), "write_wait", {"elapsed_seconds": 31})
	d.outcome == "deny"
	d.reason_codes == ["OPS-02-WAIT-LIMIT-EXCEEDED"]
}

test_wait_limit_boundary_allow if {
	d := migration.decision with input as with_detail(base("final_verify"), "write_wait", {"elapsed_seconds": 30})
	d.allowed == true
}

# deny 와 hold 가 같이 있으면 deny.
test_deny_wins_over_hold if {
	inp := with_detail(with_detail(base("final_verify"), "target_db_exposure", {"public": true}), "final_sync", {"source_writes_fenced": false, "target_applied_through_checkpoint": true, "compare_passed": true})
	d := migration.decision with input as inp
	d.outcome == "deny"
	d.reason_codes == ["OPS-01-SOURCE-NOT-FENCED", "SEC-01-PUBLIC-DB"]
}

# ---- hold ----------------------------------------------------------------

test_source_not_fenced_hold if {
	d := migration.decision with input as with_detail(base("final_verify"), "final_sync", {"source_writes_fenced": false, "target_applied_through_checkpoint": false, "compare_passed": true})
	d.outcome == "hold"
	d.allowed == false
	d.reason_codes == ["OPS-01-CHECKPOINT-NOT-APPLIED", "OPS-01-SOURCE-NOT-FENCED"]
}

test_final_sync_check_failed_is_hold if {
	d := migration.decision with input as with_check(base("final_verify"), "final_sync", check("fail", {}))
	d.outcome == "hold"
	d.reason_codes == ["OPS-01-CHECK-FAILED"]
}

test_missing_evidence_hold if {
	inp := without_check(base("pre_cutover"), "target_app_db")
	d := migration.decision with input as inp
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-MISSING"]
	some v in d.violations
	v.rule == "APP-01"
}

test_no_checks_at_all_hold if {
	inp := set_checks(base("pre_cutover"), {})
	d := migration.decision with input as inp
	d.outcome == "hold"
	count(d.violations) == 3
}

test_stale_evidence_hold if {
	c := object.union(all_checks.target_db_exposure, {"observed_at": stale})
	d := migration.decision with input as with_check(base("pre_start"), "target_db_exposure", c)
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-STALE"]
}

test_unknown_status_hold if {
	d := migration.decision with input as with_check(base("pre_start"), "target_db_exposure", check("unknown", {}))
	d.outcome == "hold"
	d.reason_codes == ["SEC-01-UNVERIFIED"]
}

test_error_status_hold if {
	d := migration.decision with input as with_check(base("pre_cutover"), "target_app_db", check("error", {}))
	d.outcome == "hold"
	d.reason_codes == ["APP-01-UNVERIFIED"]
}

# 문자열 "true" 는 boolean 이 아니다.
test_string_boolean_is_invalid_hold if {
	d := migration.decision with input as with_detail(base("pre_start"), "target_db_exposure", {"public": "false"})
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}

test_missing_fact_is_invalid_hold if {
	d := migration.decision with input as with_detail(base("pre_cutover"), "target_app_db", {"db_connected": true})
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}

test_bad_observed_at_invalid_hold if {
	c := object.union(all_checks.target_db_exposure, {"observed_at": "yesterday"})
	d := migration.decision with input as with_check(base("pre_start"), "target_db_exposure", c)
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}

test_missing_source_invalid_hold if {
	c := object.remove(all_checks.target_db_exposure, ["source"])
	d := migration.decision with input as with_check(base("pre_start"), "target_db_exposure", c)
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}

test_unknown_check_status_value_invalid_hold if {
	d := migration.decision with input as with_check(base("pre_start"), "target_db_exposure", check("ok", {"public": false}))
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}

# 평가 뒤 계획이 바뀌면 옛 증거를 재사용할 수 없다.
test_plan_digest_mismatch_hold if {
	inp := object.union(base("pre_cutover"), {"plan": {"digest": "sha256:plan-2"}})
	d := migration.decision with input as inp
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-PLAN-MISMATCH"]
}

# ---- invalid -------------------------------------------------------------

test_unsupported_schema_invalid if {
	d := migration.decision with input as object.union(base("plan"), {"schema_version": "2"})
	d.outcome == "invalid"
	d.allowed == false
	d.reason_codes == ["SCHEMA-UNSUPPORTED"]
}

test_unknown_stage_invalid if {
	d := migration.decision with input as object.union(base("plan"), {"stage": "cutover"})
	d.outcome == "invalid"
	d.reason_codes == ["STAGE-UNKNOWN"]
}

test_missing_limits_invalid if {
	inp := object.union(base("final_verify"), {"plan": {"limits": {"evidence_ttl_seconds": 300, "write_wait_max_seconds": "30"}}})
	d := migration.decision with input as inp
	d.outcome == "invalid"
	d.reason_codes == ["LIMIT-MISSING"]
}

test_missing_approved_locations_invalid if {
	inp := object.union(base("plan"), {"plan": {"approved_locations": "ap-northeast-2"}})
	d := migration.decision with input as inp
	d.outcome == "invalid"
	d.reason_codes == ["PLAN-APPROVED-LOCATIONS-MISSING"]
}

test_missing_now_invalid if {
	inp := object.remove(base("plan"), ["now"])
	d := migration.decision with input as inp
	d.outcome == "invalid"
	d.reason_codes == ["NOW-INVALID"]
}

test_empty_input_invalid if {
	d := migration.decision with input as {}
	d.outcome == "invalid"
	d.allowed == false
	count(d.reason_codes) >= 5
}

# invalid 일 때는 증거 판정을 섞지 않는다 (호출자 버그와 정책 위반을 구분).
test_invalid_does_not_mix_evidence_violations if {
	inp := object.union(with_detail(base("pre_start"), "target_db_exposure", {"public": true}), {"schema_version": "0"})
	d := migration.decision with input as inp
	d.outcome == "invalid"
	d.reason_codes == ["SCHEMA-UNSUPPORTED"]
}

# ---- allowed 는 outcome=allow 와 항상 일치 --------------------------------

test_allowed_only_when_allow if {
	every inp in [
		with_detail(base("pre_start"), "target_db_exposure", {"public": true}),
		with_check(base("pre_start"), "target_db_exposure", check("unknown", {})),
		object.union(base("plan"), {"stage": "x"}),
	] {
		d := migration.decision with input as inp
		d.allowed == false
		d.outcome != "allow"
	}
}

test_missing_public_fact_is_invalid_hold if {
	d := migration.decision with input as with_detail(base("pre_start"), "target_db_exposure", {})
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}

test_missing_final_sync_fact_is_invalid_hold if {
	d := migration.decision with input as with_detail(base("final_verify"), "final_sync", {"source_writes_fenced": true})
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}

test_missing_elapsed_is_invalid_hold if {
	d := migration.decision with input as with_detail(base("final_verify"), "write_wait", {})
	d.outcome == "hold"
	d.reason_codes == ["EVIDENCE-INVALID"]
}
