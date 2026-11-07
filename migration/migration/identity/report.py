"""Review-oriented Markdown; no raw exception, password, token or secret payloads."""

from __future__ import annotations

import json


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def markdown(plan, state=None, verification=None):
    lines = [
        "# 계정·권한 이관 검토 보고서",
        "",
        f"계획 식별값: `{plan['plan_hash']}`",
        "",
        "대응표는 검토된 대상 권한의 재구성입니다. AWS 전체 실효 권한과의 등가성을 증명하지 않습니다.",
        "",
        "| 원본 계정 | 주체 | 업무 / 환경 | 대상 그룹 | 검토 기록 |",
        "|---|---|---|---|---|",
    ]
    for row in plan["mapping_rows"]:
        source = row["source"]
        lines.append(
            "| "
            + " | ".join(
                cell(x)
                for x in (
                    source["account_id"],
                    source["principal_type"] + ":" + source["principal_id"],
                    row["business"] + " / " + row["environment"],
                    row["target_group"],
                    row["review_ref"],
                )
            )
            + " |"
        )
    lines += [
        "",
        "## 사용자 대응",
        "",
        "| 원본 사용자 ID | 원본 이름 | 대상 로그인 이름 | 등록 상태 |",
        "|---|---|---|---|",
    ]
    for row in plan.get("identity_rows", []):
        lines.append(
            "| "
            + " | ".join(
                cell(row[k])
                for k in (
                    "source_user_id",
                    "source_username",
                    "target_username",
                    "onboarding",
                )
            )
            + " |"
        )
    lines += [
        "",
        "## 적용 차단",
        "",
        "```json",
        json.dumps(plan["blockers"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 명시적 제외",
        "",
        "```json",
        json.dumps(plan["exclusions"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 별도 대응이 필요한 IAM 주체",
        "",
        "```json",
        json.dumps(
            plan.get("deferred_iam_principals", []), ensure_ascii=False, indent=2
        ),
        "```",
        "",
        "## 외부 반영·등록 필요 사항",
        "",
    ]
    lines += [
        "- " + cell(h["kind"]) + ": " + cell(h["status"]) for h in plan["handoffs"]
    ]
    lines += [
        "",
        "## 구성 적용 기록",
        "",
        "```json",
        json.dumps(state or {"status": "not_run"}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 접근 검증",
        "",
        "```json",
        json.dumps(verification or {"status": "not_run"}, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)
