# 이전 허용·보류·거절 정책 (1차). 입력 계약은 schema/input.schema.json, 예시는 examples/.
#
# 판정 원칙
#   - 정보 부족·형식 오류·오래된 증거는 절대 allow 가 아니다 (hold 또는 invalid).
#   - 수집기의 status 가 pass 여도 detail 의 사실이 어긋나면 막는다 (수집기 말만 믿지 않는다).
#   - 수치(증거 유효기간, 쓰기 대기 상한, 승인 위치)는 여기 적지 않고 input.plan 에서 받는다.
#     팀이 확정하지 않은 값을 정책이 임의로 정하지 않기 위해서다.
#   - 결과는 decision 객체 하나로 읽는다. HTTP 200 이나 allow 필드 하나만으로 판단하지 않는다.
#
# 결과(outcome)
#   allow   모든 적용 규칙을 통과. 이때만 decision.allowed == true
#   deny    조직 규칙 위반. 설정을 고치고 새 증거로 다시 판정한다
#   hold    증거 부족·오래됨·미확인·동기화 미완료. 재수집 후 다시 판정한다
#   invalid 요청 자체가 계약에 맞지 않음 (schema, stage, plan 누락). 호출자 버그로 본다
package ghost.migration

policy_version := "migration-policy-v1"

schema_version := "1"

stages := {"plan", "pre_start", "pre_cutover", "final_verify"}

check_statuses := {"pass", "fail", "unknown", "error"}

# 규칙 → 필요한 검사 이름, 적용 단계, 검사 실패(status=fail) 시 효과.
rules := {
	"SEC-01": {"check": "target_db_exposure", "stages": {"pre_start", "pre_cutover", "final_verify"}, "on_fail": "deny"},
	"SEC-02": {"check": "target_location", "stages": {"plan", "pre_start", "pre_cutover"}, "on_fail": "deny"},
	"APP-01": {"check": "target_app_db", "stages": {"pre_cutover", "final_verify"}, "on_fail": "deny"},
	"OPS-01": {"check": "final_sync", "stages": {"final_verify"}, "on_fail": "hold"},
	"OPS-02": {"check": "write_wait", "stages": {"final_verify"}, "on_fail": "deny"},
}

# ---------------------------------------------------------------------------
# 최종 판정
# ---------------------------------------------------------------------------

decision := {
	"policy_version": policy_version,
	"schema_version": schema_version,
	"job_id": object.get(input, "job_id", null),
	"stage": object.get(input, "stage", null),
	"outcome": outcome,
	"allowed": outcome == "allow",
	"reason_codes": sort({v.code | some v in violations}),
	"violations": sort(violations),
	"evaluated_rules": sort({rule | some rule, spec in rules; applies(rule)}),
	"required_checks": sort(required_checks),
	"plan_digest": object.get(input, ["plan", "digest"], null),
	"evidence_snapshot_id": object.get(input, ["evidence", "snapshot_id"], null),
}

outcome := "invalid" if has_effect("invalid")

outcome := "deny" if {
	not has_effect("invalid")
	has_effect("deny")
}

outcome := "hold" if {
	not has_effect("invalid")
	not has_effect("deny")
	has_effect("hold")
}

outcome := "allow" if {
	request_ok
	count(violations) == 0
}

has_effect(effect) if {
	some v in violations
	v.effect == effect
}

violation(code, rule, effect, message) := {"code": code, "rule": rule, "effect": effect, "message": message}

# ---------------------------------------------------------------------------
# 요청 계약 검사 (effect=invalid)
# ---------------------------------------------------------------------------

request_ok if count(request_errors) == 0

violations contains v if some v in request_errors

request_errors contains violation("SCHEMA-UNSUPPORTED", "REQUEST", "invalid", "schema_version 은 \"1\" 이어야 한다") if {
	object.get(input, "schema_version", null) != schema_version
}

request_errors contains violation("STAGE-UNKNOWN", "REQUEST", "invalid", "stage 는 plan|pre_start|pre_cutover|final_verify 중 하나다") if {
	not stages[object.get(input, "stage", null)]
}

request_errors contains violation("JOB-ID-MISSING", "REQUEST", "invalid", "job_id 는 비어 있지 않은 문자열이어야 한다") if {
	not nonempty_string(object.get(input, "job_id", null))
}

request_errors contains violation("NOW-INVALID", "REQUEST", "invalid", "now 는 RFC3339 시각이어야 한다") if {
	not parseable_time(object.get(input, "now", null))
}

request_errors contains violation("PLAN-DIGEST-MISSING", "REQUEST", "invalid", "plan.digest 는 비어 있지 않은 문자열이어야 한다") if {
	not nonempty_string(object.get(input, ["plan", "digest"], null))
}

request_errors contains violation("LIMIT-MISSING", "REQUEST", "invalid", "plan.limits.evidence_ttl_seconds 는 0보다 큰 숫자여야 한다") if {
	not positive_number(object.get(input, ["plan", "limits", "evidence_ttl_seconds"], null))
}

request_errors contains violation("LIMIT-MISSING", "REQUEST", "invalid", "plan.limits.write_wait_max_seconds 는 0보다 큰 숫자여야 한다") if {
	applies("OPS-02")
	not positive_number(object.get(input, ["plan", "limits", "write_wait_max_seconds"], null))
}

request_errors contains violation("PLAN-APPROVED-LOCATIONS-MISSING", "REQUEST", "invalid", "plan.approved_locations 는 문자열 배열이어야 한다") if {
	applies("SEC-02")
	not string_array(object.get(input, ["plan", "approved_locations"], null))
}

request_errors contains violation("EVIDENCE-SNAPSHOT-MISSING", "REQUEST", "invalid", "evidence.snapshot_id 는 비어 있지 않은 문자열이어야 한다") if {
	not nonempty_string(object.get(input, ["evidence", "snapshot_id"], null))
}

# ---------------------------------------------------------------------------
# 증거 공통 검사 (effect=hold)
# ---------------------------------------------------------------------------

checks := object.get(input, ["evidence", "checks"], {})

applies(rule) if rules[rule].stages[input.stage]

required_checks contains name if {
	some rule, spec in rules
	applies(rule)
	name := spec.check
}

rule_for_check(name) := rule if {
	some rule, spec in rules
	spec.check == name
}

# 증거 묶음이 어느 계획을 보고 수집됐는지. 계획이 바뀌면 옛 증거를 재사용하지 못한다.
violations contains violation("EVIDENCE-PLAN-MISMATCH", "EVIDENCE", "hold", "증거가 현재 계획 digest 와 다른 계획에서 수집됐다") if {
	request_ok
	object.get(input, ["evidence", "plan_digest"], null) != input.plan.digest
}

violations contains violation("EVIDENCE-MISSING", rule_for_check(name), "hold", sprintf("검사 %q 결과가 없다", [name])) if {
	request_ok
	some name in required_checks
	not checks[name]
}

violations contains violation("EVIDENCE-INVALID", rule_for_check(name), "hold", sprintf("검사 %q 결과의 형식이 계약과 다르다 (status/observed_at/source/evidence_id/detail)", [name])) if {
	request_ok
	some name in required_checks
	checks[name]
	not check_valid(name)
}

violations contains violation("EVIDENCE-STALE", rule_for_check(name), "hold", sprintf("검사 %q 결과가 evidence_ttl_seconds 보다 오래됐다", [name])) if {
	request_ok
	some name in required_checks
	check_valid(name)
	not check_fresh(name)
}

violations contains violation(sprintf("%s-UNVERIFIED", [rule]), rule, "hold", sprintf("검사 %q 를 수행하지 못했다 (status=%s)", [name, checks[name].status])) if {
	request_ok
	some name in required_checks
	check_fresh(name)
	checks[name].status in {"unknown", "error"}
	rule := rule_for_check(name)
}

violations contains violation(sprintf("%s-CHECK-FAILED", [rule]), rule, rules[rule].on_fail, sprintf("검사 %q 가 실패했다", [name])) if {
	request_ok
	some name in required_checks
	check_fresh(name)
	checks[name].status == "fail"
	rule := rule_for_check(name)
}

check_valid(name) if {
	c := checks[name]
	is_object(c)
	is_string(c.status)
	check_statuses[c.status]
	parseable_time(c.observed_at)
	nonempty_string(c.source)
	nonempty_string(c.evidence_id)
	is_object(c.detail)
}

check_fresh(name) if {
	check_valid(name)
	now_ns := time.parse_rfc3339_ns(input.now)
	observed_ns := time.parse_rfc3339_ns(checks[name].observed_at)
	ttl_ns := input.plan.limits.evidence_ttl_seconds * 1000000000
	now_ns - observed_ns <= ttl_ns
}

# 수집기가 관측에 성공(pass)했을 때만 detail 의 사실을 평가한다.
check_passed(name) if {
	request_ok
	check_fresh(name)
	checks[name].status == "pass"
}

# 없는 키는 null. (함수 호출은 not 바깥으로 끌어올려지므로 undefined 로 두면 "없음" 을 잡지 못한다.)
fact(name, key) := object.get(checks[name].detail, key, null)

# ---------------------------------------------------------------------------
# SEC-01 대상 DB 가 외부에 노출되지 않을 것
# ---------------------------------------------------------------------------

violations contains violation("EVIDENCE-INVALID", "SEC-01", "hold", "target_db_exposure.detail.public 은 boolean 이어야 한다") if {
	applies("SEC-01")
	check_passed("target_db_exposure")
	not is_boolean(fact("target_db_exposure", "public"))
}

violations contains violation("SEC-01-PUBLIC-DB", "SEC-01", "deny", "대상 DB 가 승인되지 않은 외부 경로로 노출돼 있다") if {
	applies("SEC-01")
	check_passed("target_db_exposure")
	fact("target_db_exposure", "public") == true
}

# ---------------------------------------------------------------------------
# SEC-02 대상 데이터 위치가 계획의 승인 목록 안일 것
# ---------------------------------------------------------------------------

violations contains violation("EVIDENCE-INVALID", "SEC-02", "hold", "target_location.detail.location 은 문자열이어야 한다") if {
	applies("SEC-02")
	check_passed("target_location")
	not nonempty_string(fact("target_location", "location"))
}

violations contains violation("SEC-02-UNAPPROVED-LOCATION", "SEC-02", "deny", sprintf("대상 위치 %q 는 승인 목록에 없다", [loc])) if {
	applies("SEC-02")
	check_passed("target_location")
	loc := fact("target_location", "location")
	nonempty_string(loc)
	not approved_location(loc)
}

approved_location(loc) if {
	some allowed in input.plan.approved_locations
	allowed == loc
}

# ---------------------------------------------------------------------------
# APP-01 대상 앱이 대상 DB 에 연결되고 주문 조회가 동작할 것 (healthz 200 만으로 통과시키지 않는다)
# ---------------------------------------------------------------------------

violations contains violation("EVIDENCE-INVALID", "APP-01", "hold", "target_app_db.detail.db_connected / order_query_ok 는 boolean 이어야 한다") if {
	applies("APP-01")
	check_passed("target_app_db")
	some key in {"db_connected", "order_query_ok"}
	not is_boolean(fact("target_app_db", key))
}

violations contains violation("APP-01-TARGET-DB-UNREACHABLE", "APP-01", "deny", "대상 앱이 대상 DB 에 연결하지 못한다") if {
	applies("APP-01")
	check_passed("target_app_db")
	fact("target_app_db", "db_connected") == false
}

violations contains violation("APP-01-ORDER-CHECK-FAILED", "APP-01", "deny", "대상 앱의 주문 조회 검사가 실패했다") if {
	applies("APP-01")
	check_passed("target_app_db")
	fact("target_app_db", "order_query_ok") == false
}

# ---------------------------------------------------------------------------
# OPS-01 최종 동기화 증거: 원본 쓰기 차단 → 기준점까지 대상 반영 → 전체 비교 통과
# ---------------------------------------------------------------------------

ops01_facts := {
	"source_writes_fenced": "OPS-01-SOURCE-NOT-FENCED",
	"target_applied_through_checkpoint": "OPS-01-CHECKPOINT-NOT-APPLIED",
	"compare_passed": "OPS-01-COMPARE-NOT-PASSED",
}

violations contains violation("EVIDENCE-INVALID", "OPS-01", "hold", sprintf("final_sync.detail.%s 는 boolean 이어야 한다", [key])) if {
	applies("OPS-01")
	check_passed("final_sync")
	some key, _ in ops01_facts
	not is_boolean(fact("final_sync", key))
}

violations contains violation(code, "OPS-01", "hold", sprintf("최종 동기화 조건 %s 가 아직 충족되지 않았다", [key])) if {
	applies("OPS-01")
	check_passed("final_sync")
	some key, code in ops01_facts
	fact("final_sync", key) == false
}

# ---------------------------------------------------------------------------
# OPS-02 쓰기 대기 시간이 계획의 상한 안일 것
# ---------------------------------------------------------------------------

violations contains violation("EVIDENCE-INVALID", "OPS-02", "hold", "write_wait.detail.elapsed_seconds 는 0 이상의 숫자여야 한다") if {
	applies("OPS-02")
	check_passed("write_wait")
	not nonnegative_number(fact("write_wait", "elapsed_seconds"))
}

violations contains violation("OPS-02-WAIT-LIMIT-EXCEEDED", "OPS-02", "deny", sprintf("쓰기 대기 %v초가 상한 %v초를 넘었다", [elapsed, limit])) if {
	applies("OPS-02")
	check_passed("write_wait")
	elapsed := fact("write_wait", "elapsed_seconds")
	nonnegative_number(elapsed)
	limit := input.plan.limits.write_wait_max_seconds
	elapsed > limit
}

# ---------------------------------------------------------------------------
# 타입 헬퍼
# ---------------------------------------------------------------------------

nonempty_string(x) if {
	is_string(x)
	x != ""
}

positive_number(x) if {
	is_number(x)
	x > 0
}

nonnegative_number(x) if {
	is_number(x)
	x >= 0
}

string_array(x) if {
	is_array(x)
	every item in x {
		is_string(item)
	}
}

parseable_time(x) if {
	is_string(x)
	time.parse_rfc3339_ns(x)
}
