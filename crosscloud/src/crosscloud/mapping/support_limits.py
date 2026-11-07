"""원가연 전환 도구(migration.cloud)의 지원 제한에 걸리는 스키마를 찾는다.

근거는 migration/CLOUD_MIGRATION.md 「지원 범위와 사전 준비」 절이다. 그 도구는
PRECHECK 에서 아래를 거절하는데, crosscloud 는 대상 인프라를 깔기 전 파악
단계에서 미리 찾아 사람 결정(transition_support_limit)으로 올린다(계획 편차 E7).

  * 대상 FK · 사용자 트리거(테이블별 sink 순서가 달라서)
  * 파티션·상속 · RLS · 배열·사용자 타입 · generated/identity ALWAYS
  * 소유 시퀀스의 increment≠1 · cycle
  * PK 없는 테이블(PK 기반 upsert·비교)

입력은 `pg_dump --schema-only` 같은 DDL 문자열이다. SQL 을 완전히 해석하지
않고 문장 단위 정규식으로 찾는다. 놓친 것은 원가연 PRECHECK 가 마지막으로
거절한다 — 여기는 일찍 알리는 자리이지 마지막 관문이 아니다.

  limits = support_limits.find_support_limits(schema_sql)
"""

import dataclasses
import re
from typing import Literal

SupportLimitKind = Literal[
    "foreign_key",
    "user_trigger",
    "partition_or_inheritance",
    "row_level_security",
    "array_or_user_type",
    "generated_always",
    "sequence_unsupported",
    "missing_primary_key",
    "no_tables",
]


@dataclasses.dataclass(frozen=True)
class SupportLimit:
    """지원 제한 한 건.

    Attributes:
        kind: 걸린 제한 종류.
        subject: 걸린 객체의 `schema.name`(소문자). 스키마 전체면 "schema".
    """

    kind: SupportLimitKind
    subject: str


# 문자열·주석은 지운다. 그 안의 단어(예: DEFAULT 'REFERENCES')를 잡지 않게.
_STRING_OR_COMMENT = re.compile(
    r"\$(\w*)\$.*?\$\1\$|'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S
)
_NAME = r'((?:"[^"]+"|[A-Za-z_][\w$]*)(?:\.(?:"[^"]+"|[A-Za-z_][\w$]*))?)'
_CREATE_TABLE = re.compile(
    r"^CREATE (?:(?:GLOBAL |LOCAL )?(?:TEMP|TEMPORARY|UNLOGGED) )?TABLE "
    rf"(?:IF NOT EXISTS )?{_NAME}(.*)$",
    re.I,
)
_ALTER_TABLE = re.compile(
    rf"^ALTER TABLE (?:IF EXISTS )?(?:ONLY )?{_NAME}(.*)$", re.I
)
_CREATE_TRIGGER = re.compile(
    r"^CREATE (?:OR REPLACE )?(?:CONSTRAINT )?TRIGGER "
    rf".*? ON (?:ONLY )?{_NAME}",
    re.I,
)
_CREATE_POLICY = re.compile(rf"^CREATE POLICY \S+ ON {_NAME}", re.I)
_CREATE_TYPE = re.compile(rf"^CREATE (?:TYPE|DOMAIN) {_NAME}", re.I)
_CREATE_SEQUENCE = re.compile(
    rf"^CREATE SEQUENCE (?:IF NOT EXISTS )?{_NAME}(.*)$", re.I
)

_FOREIGN_KEY = re.compile(r"\bREFERENCES\b|\bFOREIGN KEY\b", re.I)
_PRIMARY_KEY = re.compile(r"\bPRIMARY KEY\b", re.I)
_PARTITION = re.compile(
    r"\bPARTITION (?:BY|OF)\b|\bINHERITS\b|\bATTACH PARTITION\b", re.I
)
_ARRAY = re.compile(r"\[\s*\d*\s*\]|\bARRAY\b", re.I)
_GENERATED_ALWAYS = re.compile(r"\bGENERATED ALWAYS\b", re.I)
_ROW_LEVEL_SECURITY = re.compile(
    r"\b(?:ENABLE|FORCE) ROW LEVEL SECURITY\b", re.I
)
_INCREMENT = re.compile(r"\bINCREMENT (?:BY )?(-?\d+)", re.I)
_CYCLE = re.compile(r"(?<!NO )\bCYCLE\b", re.I)


def find_support_limits(schema_sql: str) -> tuple[SupportLimit, ...]:
    """DDL 에서 지원 제한에 걸리는 곳을 찾는다.

    Args:
        schema_sql: 앱 테이블을 만드는 DDL 전체.

    Returns:
        찾은 제한(중복 없음, 처음 나온 순서). 걸리는 것이 없으면 빈 tuple.
        테이블이 하나도 없으면 판정할 수 없으므로 no_tables 를 더한다.
    """
    found: dict[SupportLimit, None] = {}
    tables: dict[str, bool] = {}
    for statement in _statements(schema_sql):
        for limit in _limits_in(statement, tables):
            found[limit] = None
    if not tables:
        found[SupportLimit("no_tables", "schema")] = None
    for table, has_primary_key in tables.items():
        if not has_primary_key:
            found[SupportLimit("missing_primary_key", table)] = None
    return tuple(found)


def _statements(schema_sql: str) -> list[str]:
    def blank(match: re.Match[str]) -> str:
        return "''" if match.group(0).startswith(("'", "$")) else " "

    cleaned = _STRING_OR_COMMENT.sub(blank, schema_sql)
    return [
        " ".join(part.split()) for part in cleaned.split(";") if part.strip()
    ]


def _limits_in(statement: str, tables: dict[str, bool]) -> list[SupportLimit]:
    if match := _CREATE_TABLE.match(statement):
        table = _normalize(match.group(1))
        tables[table] = bool(_PRIMARY_KEY.search(match.group(2)))
        return _table_limits(table, match.group(2))
    if match := _ALTER_TABLE.match(statement):
        table = _normalize(match.group(1))
        if _PRIMARY_KEY.search(match.group(2)):
            tables[table] = True
        return _table_limits(table, match.group(2))
    if match := _CREATE_TRIGGER.match(statement):
        return [SupportLimit("user_trigger", _normalize(match.group(1)))]
    if match := _CREATE_POLICY.match(statement):
        return [SupportLimit("row_level_security", _normalize(match.group(1)))]
    if match := _CREATE_TYPE.match(statement):
        return [SupportLimit("array_or_user_type", _normalize(match.group(1)))]
    if match := _CREATE_SEQUENCE.match(statement):
        return _sequence_limits(_normalize(match.group(1)), match.group(2))
    return []


def _table_limits(table: str, body: str) -> list[SupportLimit]:
    checks: tuple[tuple[re.Pattern[str], SupportLimitKind], ...] = (
        (_FOREIGN_KEY, "foreign_key"),
        (_PARTITION, "partition_or_inheritance"),
        (_ROW_LEVEL_SECURITY, "row_level_security"),
        (_ARRAY, "array_or_user_type"),
        (_GENERATED_ALWAYS, "generated_always"),
    )
    limits = [
        SupportLimit(kind, table)
        for pattern, kind in checks
        if pattern.search(body)
    ]
    return limits + _sequence_limits(table, body)


def _sequence_limits(subject: str, body: str) -> list[SupportLimit]:
    increments = [int(value) for value in _INCREMENT.findall(body)]
    if any(value != 1 for value in increments) or _CYCLE.search(body):
        return [SupportLimit("sequence_unsupported", subject)]
    return []


def _normalize(name: str) -> str:
    parts = [part.strip('"').lower() for part in name.split(".")]
    if len(parts) == 1:
        parts.insert(0, "public")
    return ".".join(parts)
