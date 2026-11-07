from __future__ import annotations

import json

from ..identity.report import cell


def row(values):
    return "| " + " | ".join(cell(v) for v in values) + " |"


def source_label(src):
    if src["kind"] == "group":
        return "group " + src["path"]
    if src["kind"] == "realm_role":
        return "realm role " + src["role"]
    return "client role " + src["client"] + "/" + src["role"]


def block(value):
    return ["```json", json.dumps(value, ensure_ascii=False, indent=2), "```", ""]


def markdown(plan, state=None, verification=None):
    lines = [
        "# Keycloak → IAM Identity Center 계정·권한 이관 검토 보고서",
        "",
        f"계획 식별값: `{plan['plan_hash']}`",
        "",
        "비밀번호·다중 인증(MFA)·Keycloak 자격증명은 이관하지 않습니다. 새로 만든 사용자는 모두 IAM Identity Center 등록 절차(이메일 확인 초대·비밀번호 설정·MFA 등록)를 마쳐야 로그인할 수 있습니다.",
        "",
        "대응표에 없는 그룹·역할은 추측하지 않고 제외합니다. 아래 대응은 운영자가 검토한 할당이며 Keycloak 권한과의 의미 등가성을 증명하지 않습니다.",
        "",
        "## 그룹·역할 대응",
        "",
        "| Keycloak 원본 | 대상 그룹 | 구성원 수 | AWS 계정 / 권한 세트 | 검토 기록 |",
        "|---|---|---|---|---|",
    ]
    for m in plan["mapping_rows"]:
        targets = ", ".join(
            a["account_id"] + " / " + a["permission_set_name"] for a in m["assignments"]
        )
        lines.append(
            row(
                (
                    source_label(m["source"]),
                    m["target_group"],
                    len(m["members"]),
                    targets or "-",
                    m["review_ref"] or "-",
                )
            )
        )
    lines += [
        "",
        "## 사용자",
        "",
        "| 사용자 이름 | 이메일 | 외부 연동 사용자 | 계획 | 등록 절차 |",
        "|---|---|---|---|---|",
    ]
    for u in plan["user_rows"]:
        lines.append(
            row(
                (
                    u["username"],
                    u["email"] or "-",
                    "예" if u["federated"] else "아니오",
                    u["action"],
                    u["onboarding"],
                )
            )
        )
    lines += ["", "## 충돌", ""] + block(plan["conflicts"])
    lines += ["## 적용 차단", ""] + block(plan["blockers"])
    lines += ["## 명시적 제외", ""] + block(plan["exclusions"])
    lines += ["## 외부 절차", ""]
    lines += [
        "- " + cell(h["kind"]) + ": " + cell(h["status"]) for h in plan["handoffs"]
    ]
    lines += ["", "## 적용 기록", ""] + block(state or {"status": "not_run"})
    lines += ["## 구성 검증", ""] + block(verification or {"status": "not_run"})
    return "\n".join(lines)
