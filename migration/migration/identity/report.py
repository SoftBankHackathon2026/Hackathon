"""Reports distinguish configuration changes, evidence and real access tests."""
from .models import Plan, primitive


def report(plan: Plan, state=None) -> dict:
    execution = state.results(plan.plan_digest) if state else []
    verification = state.verification(plan.plan_digest) if state else None
    configured = len(execution) == len(plan.operations) and bool(execution) and all(x["status"] in {"applied", "reconciled", "reused"} for x in execution)
    return {"plan_digest": plan.plan_digest, "rule_version": plan.rule_version,
            "direction": plan.spec.direction, "configuration_status": "blocked" if plan.blockers else "applied_configuration" if configured else "partially_applied" if execution else "planned",
            "live_access_verified": bool(verification and verification.get("live_access_verified")), "blockers": plan.blockers, "warnings": plan.warnings,
            "coverage": {"source": plan.inventory.source["coverage"], "target": plan.inventory.target["coverage"]},
            "operations": primitive(plan.operations), "execution": execution, "verification": verification,
            "access_changes": plan.graph.get("access_changes", {}),
            "remaining": ["review prerequisite execution identity attachment", "complete actual subject allow/deny and revocation tests",
                          "resolve all unsupported or missing policy contexts"],
            "support": {"aws-to-gcp": "narrow object-read plan and additive adapter; live policy gaps block apply",
                        "gcp-to-aws": "separate narrow object-read rule and additive adapter; live policy gaps block apply",
                        "federation-direct": "unsupported", "federation-impersonation": "unsupported",
                        "keycloak-to-kubernetes": "explicit pod-read entitlement plans and isolated adapters; live policy gaps block apply",
                        "openstack": "scoped assignment adapter only; federation translation unsupported",
                        "onprem-to-cloud": "unsupported without product-specific task mapping"}}


def markdown(value: dict) -> str:
    def safe(text):
        return str(text).replace("`", "\\`").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
    lines = ["# 계정·권한 이관 보고", "", f"계획 식별값: `{value['plan_digest']}`", "",
             f"구성 상태: {safe(value['configuration_status'])}", f"실제 접근 검증 완료: {'예' if value['live_access_verified'] else '아니요'}", "", "## 차단 사유", ""]
    lines += [f"- {safe(x)}" for x in value["blockers"]] or ["- 없음 (실제 접근 검증을 뜻하지 않습니다.)"]
    lines += ["", "## 지원 범위", ""] + [f"- {safe(k)}: {safe(v)}" for k, v in value["support"].items()]
    lines += ["", "## 항목별 실행 결과", ""] + [f"- {safe(x['operation_id'])}: {safe(x['status'])}; 소유 구분: {safe(x.get('ownership','unknown'))}" for x in value["execution"]]
    verification = value.get("verification")
    lines += ["", "## 실제 주체 접근 시험", ""]
    if verification:
        lines += [f"시험 방식: {safe(verification['mode'])}"]
        lines += [f"- {safe(x['id'])}: {safe(x['status'])}; 실제 결과: {safe(x['actual'])}" for x in verification["results"]]
    else:
        lines += ["실행 기록이 없습니다."]
    lines += ["", "## 남은 작업", ""] + [f"- {safe(x)}" for x in value["remaining"]]
    return "\n".join(lines) + "\n"
