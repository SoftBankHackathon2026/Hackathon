# AI 수정안(remediation) 적용 허용 정책. 입력 계약은 schema/remediation.schema.json, 예시는 examples/remediation_*.json.
#
# 무엇을 판정하나
#   ai-agent 가 만든 "설정 변경 제안" 을 실제 설정 저장소(Vault / AWS Secrets Manager)에 써도 되는지.
#   마이그레이션 단계 허용(migration.rego)과는 별개의 판정이다. 제안이 allow 여도 적용 뒤 migration.rego 의
#   pre_cutover 재판정을 다시 받아야 이전이 재개된다.
#
# 판정 원칙
#   - 허용 키 밖의 변경, 비밀값 키 변경, 계획에 등록되지 않은 값은 deny (AI 가 고칠 수 있는 범위를 정책이 고정한다).
#   - 변경 뒤 연결 검증(remediation_probe)이 pass 가 아니면 hold (근거 없는 적용 금지).
#   - plan.remediation.auto_apply 가 true 가 아니면 hold + APPROVAL-REQUIRED (사람 승인 경로).
#   - 수치·허용 키·허용 값은 여기 적지 않고 input.scope / input.plan 에서 받는다.
package ghost.remediation

policy_version := "remediation-policy-v1"

schema_version := "1"

decision := {
	"policy_version": policy_version,
	"schema_version": schema_version,
	"job_id": object.get(input, "job_id", null),
	"stage": object.get(input, "stage", null),
	"outcome": outcome,
	"allowed": outcome == "allow",
	"reason_codes": sort({v.code | some v in violations}),
	"violations": sort(violations),
	"evaluated_rules": ["REM-01", "REM-02", "REM-03", "REM-04", "REM-05"],
	"required_checks": ["remediation_probe"],
	"plan_digest": object.get(input, ["plan", "digest"], null),
	"evidence_snapshot_id": object.get(input, ["evidence", "snapshot_id"], null),
	"proposal_digest": object.get(input, ["proposal", "digest"], null),
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
# 요청 계약 (effect=invalid)
# ---------------------------------------------------------------------------

request_ok if count(request_errors) == 0

violations contains v if some v in request_errors

request_errors contains violation("SCHEMA-UNSUPPORTED", "REQUEST", "invalid", "schema_version 은 \"1\" 이어야 한다") if {
	object.get(input, "schema_version", null) != schema_version
}

request_errors contains violation("STAGE-UNKNOWN", "REQUEST", "invalid", "stage 는 remediation 이어야 한다") if {
	object.get(input, "stage", null) != "remediation"
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

request_errors contains violation("PROPOSAL-MISSING", "REQUEST", "invalid", "proposal.digest 와 proposal.changes 배열이 있어야 한다") if {
	not nonempty_string(object.get(input, ["proposal", "digest"], null))
}

request_errors contains violation("PROPOSAL-MISSING", "REQUEST", "invalid", "proposal.digest 와 proposal.changes 배열이 있어야 한다") if {
	not is_array(object.get(input, ["proposal", "changes"], null))
}

request_errors contains violation("SCOPE-MISSING", "REQUEST", "invalid", "scope.allowed_keys 는 문자열 배열이어야 한다") if {
	not string_array(object.get(input, ["scope", "allowed_keys"], null))
}

request_errors contains violation("EVIDENCE-SNAPSHOT-MISSING", "REQUEST", "invalid", "evidence.snapshot_id 는 비어 있지 않은 문자열이어야 한다") if {
	not nonempty_string(object.get(input, ["evidence", "snapshot_id"], null))
}

changes := object.get(input, ["proposal", "changes"], [])

allowed_keys := {k | some k in input.scope.allowed_keys}

# 변경이 하나도 없으면 적용할 것이 없다 → deny (호출자가 적용 경로로 들어오면 안 된다).
violations contains violation("REM-00-EMPTY-PROPOSAL", "REM-00", "deny", "변경 항목이 없는 제안은 적용할 수 없다") if {
	request_ok
	count(changes) == 0
}

violations contains violation("EVIDENCE-INVALID", "REM-00", "hold", "proposal.changes 항목은 {key, old, new} 문자열이어야 한다") if {
	request_ok
	some c in changes
	not change_valid(c)
}

change_valid(c) if {
	is_object(c)
	nonempty_string(c.key)
	is_string(c.new)
}

# ---------------------------------------------------------------------------
# REM-01 허용 키 밖의 변경 금지
# ---------------------------------------------------------------------------

violations contains violation("REM-01-KEY-NOT-ALLOWED", "REM-01", "deny", sprintf("키 %q 는 AI 가 바꿀 수 있는 범위가 아니다", [c.key])) if {
	request_ok
	some c in changes
	change_valid(c)
	not allowed_keys[c.key]
}

# ---------------------------------------------------------------------------
# REM-02 값은 계획에 등록된 허용 값 안일 것 (AI 가 만든 임의 주소 금지)
# scope.allowed_values: {key: [value, ...]}. 목록이 없는 키는 값 제한 없음 (allowed_keys 로만 제한).
# ---------------------------------------------------------------------------

violations contains violation("REM-02-VALUE-NOT-APPROVED", "REM-02", "deny", sprintf("키 %q 의 값 %q 는 계획에 등록된 값이 아니다", [c.key, c.new])) if {
	request_ok
	some c in changes
	change_valid(c)
	allowed_keys[c.key]
	allowed := object.get(input, ["scope", "allowed_values", c.key], null)
	is_array(allowed)
	not value_in(c.new, allowed)
}

value_in(v, arr) if {
	some a in arr
	a == v
}

# ---------------------------------------------------------------------------
# REM-03 비밀값 키 변경 금지 (allowed_keys 에 들어 있어도 막는다)
# ---------------------------------------------------------------------------

secret_parts := {"password", "secret", "token", "dsn", "conninfo", "key"}

violations contains violation("REM-03-SECRET-KEY", "REM-03", "deny", sprintf("키 %q 는 비밀값으로 간주돼 AI 가 바꿀 수 없다", [c.key])) if {
	request_ok
	some c in changes
	change_valid(c)
	some part in secret_parts
	contains(lower(c.key), part)
}

# ---------------------------------------------------------------------------
# REM-04 변경 뒤 연결 검증이 통과했을 것 (근거 없는 적용 금지)
# ---------------------------------------------------------------------------

probe := object.get(input, ["evidence", "checks", "remediation_probe"], null)

violations contains violation("REM-04-PROBE-MISSING", "REM-04", "hold", "수정 값으로 수행한 연결 검증(remediation_probe) 결과가 없다") if {
	request_ok
	probe == null
}

violations contains violation("EVIDENCE-INVALID", "REM-04", "hold", "remediation_probe 결과의 형식이 계약과 다르다") if {
	request_ok
	probe != null
	not probe_valid
}

violations contains violation("REM-04-PROBE-STALE", "REM-04", "hold", "remediation_probe 결과가 evidence_ttl_seconds 보다 오래됐다") if {
	request_ok
	probe_valid
	not probe_fresh
}

violations contains violation("REM-04-PROBE-UNVERIFIED", "REM-04", "hold", sprintf("연결 검증을 수행하지 못했다 (status=%s)", [probe.status])) if {
	request_ok
	probe_fresh
	probe.status in {"unknown", "error"}
}

violations contains violation("REM-04-PROBE-FAILED", "REM-04", "hold", "수정 값으로도 대상 DB 에 연결되지 않는다") if {
	request_ok
	probe_fresh
	probe.status == "fail"
}

violations contains violation("REM-04-PROBE-FAILED", "REM-04", "hold", "수정 값으로도 대상 DB 에 연결되지 않는다") if {
	request_ok
	probe_fresh
	probe.status == "pass"
	object.get(probe, ["detail", "db_connected"], null) != true
}

# 제안이 검증된 설정과 같은 것인지. 검증 뒤 제안이 바뀌면 재검증한다.
violations contains violation("REM-04-PROBE-PROPOSAL-MISMATCH", "REM-04", "hold", "연결 검증이 현재 제안(digest)에 대한 것이 아니다") if {
	request_ok
	probe_fresh
	object.get(probe, ["detail", "proposal_digest"], null) != input.proposal.digest
}

probe_valid if {
	is_object(probe)
	is_string(probe.status)
	probe.status in {"pass", "fail", "unknown", "error"}
	parseable_time(probe.observed_at)
	nonempty_string(probe.source)
	nonempty_string(probe.evidence_id)
	is_object(probe.detail)
}

probe_fresh if {
	probe_valid
	now_ns := time.parse_rfc3339_ns(input.now)
	observed_ns := time.parse_rfc3339_ns(probe.observed_at)
	ttl_ns := input.plan.limits.evidence_ttl_seconds * 1000000000
	now_ns - observed_ns <= ttl_ns
}

# ---------------------------------------------------------------------------
# REM-05 자동 적용은 계획이 허용했을 때만. 아니면 사람 승인을 기다린다.
# ---------------------------------------------------------------------------

violations contains violation("APPROVAL-REQUIRED", "REM-05", "hold", "계획이 자동 적용을 허용하지 않는다. 사람 승인이 필요하다") if {
	request_ok
	object.get(input, ["plan", "remediation", "auto_apply"], false) != true
	object.get(input, ["approval", "approved"], false) != true
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
