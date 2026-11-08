"""통제 ID → 일본 클라우드 보안 기준 조항 매핑.

조항 번호는 통제 점검 코드와 따로 둔다. ISMAP 管理基準은 2027년 이후 ISO/IEC 27002:2022
체계로 바뀔 예정이라, 개정되면 이 파일의 매핑(과 version)만 바꾸면 된다.

ref 문자열은 "<표준 ID> <조항>" 형식이다. 프론트는 첫 공백 앞을 표준 ID로 묶는다.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StandardMapping:
    standard: str
    version: str
    basis: str
    clauses: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def refs(self, control_id: str) -> list[str]:
        return [f"{self.standard} {clause}" for clause in self.clauses.get(control_id, ())]


_WORKLOAD_HARDENING = ("9.4.4", "12.5.1")

ISMAP = StandardMapping(
    standard="ISMAP",
    version="2023",
    basis="ISMAP 管理基準 (令和5年9月22日最終改定, JIS Q 27002:2014 = ISO/IEC 27002:2013 번호 체계)",
    clauses={
        "NET-01": ("13.1.3",),
        "NET-02": ("13.1.1", "13.1.3"),
        "NET-03": ("13.2.1", "14.1.2"),
        "NET-04": ("13.1.2",),
        "WL-01": _WORKLOAD_HARDENING,
        "WL-02": _WORKLOAD_HARDENING,
        "WL-03": _WORKLOAD_HARDENING,
        "WL-04": _WORKLOAD_HARDENING,
        "WL-05": ("12.5.1", "14.2.4"),
        "WL-06": _WORKLOAD_HARDENING,
        "ID-01": ("9.1.2", "9.2.3"),
        "ID-02": ("9.2.3", "9.4.2"),
        "ID-03": ("9.2.4",),
        "DATA-01": ("10.1.1", "13.2.1"),
        "DATA-02": ("10.1.1",),
        "DATA-03": ("12.3.1",),
        "DATA-04": ("9.2.4", "10.1.2"),
        "DET-01": ("12.2.1", "16.1.2"),
        "DET-02": ("18.2.3",),
        "DET-03": ("12.4.1", "12.4.3"),
        "DET-04": ("12.4.1",),
        "GOV-01": ("12.1.2", "14.2.2"),
        "GOV-02": ("12.1.3",),
        "JP-01": ("18.1.1",),
        "JP-09": ("12.6.1",),
    },
)

# ISO/IEC 27017 의 CLD.* 는 클라우드 고유로 새로 생긴 7개 통제다. 기존 27002 통제에
# 클라우드 지침만 덧붙인 조항(예: 18.1.1)은 CLD 접두어 없이 원래 번호로 적는다.
ISMS_CLOUD = StandardMapping(
    standard="ISMS-CLOUD",
    version="2016",
    basis="ISMS 클라우드 보안 인증 (ISO/IEC 27017:2015, JIP-ISMS517-1.0)",
    clauses={
        "NET-01": ("CLD.9.5.1",),
        "NET-02": ("CLD.13.1.4",),
        "WL-01": ("CLD.9.5.2",),
        "WL-02": ("CLD.9.5.2",),
        "WL-03": ("CLD.9.5.2",),
        "WL-04": ("CLD.9.5.2",),
        "WL-06": ("CLD.9.5.2",),
        "ID-01": ("CLD.12.1.5",),
        "ID-02": ("CLD.12.1.5",),
        "DET-03": ("CLD.12.4.5",),
        "DET-04": ("CLD.13.1.4",),
        "JP-01": ("18.1.1",),
    },
)

MAPPINGS: tuple[StandardMapping, ...] = (ISMAP, ISMS_CLOUD)


def standard_refs(
    control_id: str,
    base: tuple[str, ...] = (),
    mappings: tuple[StandardMapping, ...] = MAPPINGS,
) -> list[str]:
    """통제에 직접 적힌 ref 뒤에 매핑 데이터의 ref 를 붙인다(중복 제거, 순서 유지)."""
    merged = list(base)
    for mapping in mappings:
        merged += [ref for ref in mapping.refs(control_id) if ref not in merged]
    return merged
