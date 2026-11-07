"""진단 결과로 원인 가설을 세우고 하나씩 맞다·아니다·모름으로 가른다.

진단은 지금 설정으로 DB 에 직접 붙어 본 결과다 (probe.classify 의 문구). 그 문구와 계획서 값 비교만 근거로 쓴다.
"""

from __future__ import annotations

from typing import Any, Mapping

from .probe import ProbeResult

VALIDATED, INVALIDATED, INCONCLUSIVE = "validated", "invalidated", "inconclusive"

TCP_CAUSES = ("dns", "refused", "tcp_timeout", "tcp_other")

_CAUSES: tuple[tuple[str, str], ...] = (
    ("DNS:", "dns"),
    ("TCP: 연결 거부", "refused"),
    ("TCP: 연결 시간 초과", "tcp_timeout"),
    ("TCP:", "tcp_other"),
    ("SQLSTATE 3D000", "no_db"),
    ("SQLSTATE 28P01", "password"),
    ("pg_hba", "blocked"),
    ("SQLSTATE 28000", "no_role"),
    ("TLS:", "tls"),
    ("SQLSTATE 57P03", "starting"),
    ("SQLSTATE 53300", "busy"),
)


def cause_of(result: ProbeResult) -> str:
    if result.ok:
        return "ok"
    detail = result.detail or ""
    for marker, cause in _CAUSES:
        if marker in detail:
            return cause
    return "tcp_other" if result.step == "tcp" else "login_other"


def _hypothesis(hid: str, title: str, key: str | None, status: str, evidence: str) -> dict[str, Any]:
    return {"id": hid, "title": title, "key": key, "status": status, "evidence": evidence}


def build_hypotheses(
    result: ProbeResult, current: Mapping[str, str], planned: Mapping[str, str],
) -> list[dict[str, Any]]:
    cause = cause_of(result)
    detail = result.detail or result.error or "-"
    reached = cause not in TCP_CAUSES
    connected = cause == "ok"

    def differs(key: str) -> bool:
        return key in planned and str(current.get(key, "")) != planned[key]

    def compare(key: str) -> str:
        if key not in planned:
            return "계획서에 목표값이 없어요."
        if differs(key):
            return f"지금 값 {current.get(key, '-')} 이 계획서 값 {planned[key]} 과 달라요."
        return "계획서 값과 같아요."

    out: list[dict[str, Any]] = []

    if reached:
        host = _hypothesis("host", "DB 주소가 틀렸다", "DB_HOST", INVALIDATED, f"TCP 연결은 됐어요. {compare('DB_HOST')}")
        port = _hypothesis("port", "포트가 틀렸다", "DB_PORT", INVALIDATED, f"TCP 연결은 됐어요. {compare('DB_PORT')}")
    else:
        host_status = VALIDATED if differs("DB_HOST") else (INVALIDATED if cause != "dns" else INCONCLUSIVE)
        host = _hypothesis("host", "DB 주소가 틀렸다", "DB_HOST", host_status, f"{detail}. {compare('DB_HOST')}")
        port_status = INCONCLUSIVE if cause == "dns" else (VALIDATED if differs("DB_PORT") else INVALIDATED)
        port = _hypothesis("port", "포트가 틀렸다", "DB_PORT", port_status,
                           "주소를 못 찾아 포트까지 가지 못했어요." if cause == "dns" else f"{detail}. {compare('DB_PORT')}")
    out += [host, port]

    if cause == "no_db":
        if differs("DB_NAME"):
            name_status, name_evidence = VALIDATED, f"{detail}. {compare('DB_NAME')}"
        else:
            name_status, name_evidence = INCONCLUSIVE, f"{detail}. 이름은 계획서와 같아 DB 가 아직 없는 것일 수 있어요."
    elif connected:
        name_status, name_evidence = INVALIDATED, "로그인과 조회까지 됐어요."
    else:
        name_status, name_evidence = INCONCLUSIVE, f"DB 이름을 확인하는 단계까지 가지 못했어요. {compare('DB_NAME')}"
    out.append(_hypothesis("name", "DB 이름이 틀렸다", "DB_NAME", name_status, name_evidence))

    if cause in ("no_role", "blocked"):
        status = VALIDATED if differs("DB_USER") else INCONCLUSIVE
        out.append(_hypothesis("user", "DB 사용자가 틀렸다", "DB_USER", status, f"{detail}. {compare('DB_USER')}"))
    elif cause in ("no_db", "ok"):
        out.append(_hypothesis("user", "DB 사용자가 틀렸다", "DB_USER", INVALIDATED, "사용자 인증은 통과했어요."))
    else:
        out.append(_hypothesis("user", "DB 사용자가 틀렸다", "DB_USER", INCONCLUSIVE,
                               f"사용자를 확인하는 단계까지 가지 못했어요. {compare('DB_USER')}"))

    if cause == "password":
        out.append(_hypothesis("password", "비밀번호가 틀렸다", None, VALIDATED, f"{detail}. 비밀번호는 AI가 다루지 않아요."))
    elif cause in ("no_db", "ok"):
        out.append(_hypothesis("password", "비밀번호가 틀렸다", None, INVALIDATED, "비밀번호 인증은 통과했어요."))
    else:
        out.append(_hypothesis("password", "비밀번호가 틀렸다", None, INCONCLUSIVE, "인증 단계까지 가지 못했어요."))

    if cause == "tls" or "DB_SSLMODE" in planned:
        if cause == "tls":
            status, evidence = VALIDATED, f"{detail}. {compare('DB_SSLMODE')}"
        elif reached:
            status, evidence = INVALIDATED, "TLS 연결은 됐어요."
        else:
            status, evidence = INCONCLUSIVE, "TLS 단계까지 가지 못했어요."
        out.append(_hypothesis("sslmode", "TLS 설정이 틀렸다", "DB_SSLMODE", status, evidence))

    if reached:
        server_status, server_evidence = INVALIDATED, "DB 서버가 응답했어요."
        if cause in ("starting", "busy"):
            server_status, server_evidence = VALIDATED, f"{detail}. 설정이 아니라 DB 쪽 상태예요."
    elif any(differs(key) for key in ("DB_HOST", "DB_PORT")):
        server_status, server_evidence = INCONCLUSIVE, "주소나 포트가 계획서와 달라 먼저 그쪽을 의심해요."
    else:
        server_status, server_evidence = VALIDATED, f"{detail}. 주소와 포트는 계획서와 같아요."
    out.append(_hypothesis("server", "DB 서버가 내려갔거나 네트워크가 막혔다", None, server_status, server_evidence))
    return out
