"""원가연 전환 도구(migration.cloud)의 지원 제한에 걸리는 스키마를 찾는다.

근거는 migration/CLOUD_MIGRATION.md 「지원 범위와 사전 준비」 절과 그 PRECHECK
구현(migration/cloud/postgres.py 의 열 타입 허용 목록)이다. 그 도구는 PRECHECK
에서 아래를 거절하는데, crosscloud 는 대상 인프라를 깔기 전 파악 단계에서 미리
찾아 사람 결정(transition_support_limit)으로 올린다(계획 편차 E7).

  * 대상 FK · 사용자 트리거(테이블별 sink 순서가 달라서)
  * 파티션·상속 · RLS · 배열·사용자 타입 · generated/identity ALWAYS
  * 허용 목록 밖의 열 타입(확장 타입 포함) · 확장(CREATE EXTENSION)
  * 소유 시퀀스의 increment≠1 · cycle, 어느 열에도 소유되지 않은 시퀀스
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
    "unsupported_column_type",
    "extension",
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
        subject: 걸린 객체의 `schema.name`(소문자). 확장이면 확장 이름,
            스키마 전체면 "schema".
    """

    kind: SupportLimitKind
    subject: str


# 원가연 PRECHECK 가 받는 열 타입(postgres.py 의 typname 목록)을 SQL 표기로
# 옮긴 것. 길이·정밀도 수식어 `(...)` 는 지운 뒤 비교한다.
_SUPPORTED_COLUMN_TYPES = frozenset(
    {
        "smallint", "int2", "smallserial", "serial2",
        "integer", "int", "int4", "serial", "serial4",
        "bigint", "int8", "bigserial", "serial8",
        "text", "varchar", "character varying",
        "char", "character", "bpchar",
        "boolean", "bool",
        "numeric", "decimal",
        "real", "float4", "double precision", "float8", "float",
        "date",
        "timestamp", "timestamp without time zone",
        "timestamptz", "timestamp with time zone",
        "time", "time without time zone",
        "uuid", "json", "jsonb", "bytea",
    }
)  # fmt: skip

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
_CREATE_EXTENSION = re.compile(
    rf"^CREATE EXTENSION (?:IF NOT EXISTS )?{_NAME}", re.I
)
_CREATE_SEQUENCE = re.compile(
    rf"^CREATE SEQUENCE (?:IF NOT EXISTS )?{_NAME}(.*)$", re.I
)
_ALTER_SEQUENCE = re.compile(
    rf"^ALTER SEQUENCE (?:IF EXISTS )?{_NAME}(.*)$", re.I
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
_OWNED_BY = re.compile(r"\bOWNED BY (\S+)", re.I)
# 테이블 본문의 원소 중 열이 아닌 것(테이블 제약·LIKE)의 첫 단어.
_TABLE_ELEMENT_KEYWORDS = frozenset(
    {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE", "LIKE"}
)
# 열 정의에서 타입이 끝나는 자리(열 제약의 첫 단어).
_COLUMN_CONSTRAINT = re.compile(
    r"\b(?:NOT|NULL|DEFAULT|PRIMARY|REFERENCES|UNIQUE|CHECK|COLLATE"
    r"|GENERATED|CONSTRAINT)\b",
    re.I,
)


@dataclasses.dataclass
class _Scan:
    """문장을 차례로 읽으며 모으는 상태. 이 모듈 안에서만 쓴다."""

    found: dict[SupportLimit, None] = dataclasses.field(default_factory=dict)
    tables: dict[str, bool] = dataclasses.field(default_factory=dict)
    sequence_owned: dict[str, bool] = dataclasses.field(default_factory=dict)

    def add(self, kind: SupportLimitKind, subject: str) -> None:
        self.found[SupportLimit(kind, subject)] = None


def find_support_limits(schema_sql: str) -> tuple[SupportLimit, ...]:
    """DDL 에서 지원 제한에 걸리는 곳을 찾는다.

    Args:
        schema_sql: 앱 테이블을 만드는 DDL 전체.

    Returns:
        찾은 제한(중복 없음, 처음 나온 순서). 걸리는 것이 없으면 빈 tuple.
        테이블이 하나도 없으면 판정할 수 없으므로 no_tables 를 더한다.
    """
    scan = _Scan()
    for statement in _statements(schema_sql):
        _read_statement(statement, scan)
    if not scan.tables:
        scan.add("no_tables", "schema")
    for table, has_primary_key in scan.tables.items():
        if not has_primary_key:
            scan.add("missing_primary_key", table)
    for sequence, owned in scan.sequence_owned.items():
        if not owned:
            scan.add("sequence_unsupported", sequence)
    return tuple(scan.found)


def _statements(schema_sql: str) -> list[str]:
    def blank(match: re.Match[str]) -> str:
        return "''" if match.group(0).startswith(("'", "$")) else " "

    cleaned = _STRING_OR_COMMENT.sub(blank, schema_sql)
    return [
        " ".join(part.split()) for part in cleaned.split(";") if part.strip()
    ]


def _read_statement(statement: str, scan: _Scan) -> None:
    if match := _CREATE_TABLE.match(statement):
        table, body = _normalize(match.group(1)), match.group(2)
        scan.tables[table] = bool(_PRIMARY_KEY.search(body))
        _check_table_body(table, body, scan)
        if not _ARRAY.search(body) and _has_unsupported_column(body):
            scan.add("unsupported_column_type", table)
    elif match := _ALTER_TABLE.match(statement):
        table, body = _normalize(match.group(1)), match.group(2)
        if _PRIMARY_KEY.search(body):
            scan.tables[table] = True
        _check_table_body(table, body, scan)
    elif match := _CREATE_TRIGGER.match(statement):
        scan.add("user_trigger", _normalize(match.group(1)))
    elif match := _CREATE_POLICY.match(statement):
        scan.add("row_level_security", _normalize(match.group(1)))
    elif match := _CREATE_TYPE.match(statement):
        scan.add("array_or_user_type", _normalize(match.group(1)))
    elif match := _CREATE_EXTENSION.match(statement):
        scan.add("extension", match.group(1).strip('"').lower())
    elif match := _CREATE_SEQUENCE.match(statement):
        _read_sequence(_normalize(match.group(1)), match.group(2), scan)
    elif match := _ALTER_SEQUENCE.match(statement):
        _read_sequence(_normalize(match.group(1)), match.group(2), scan)


def _check_table_body(table: str, body: str, scan: _Scan) -> None:
    checks: tuple[tuple[re.Pattern[str], SupportLimitKind], ...] = (
        (_FOREIGN_KEY, "foreign_key"),
        (_PARTITION, "partition_or_inheritance"),
        (_ROW_LEVEL_SECURITY, "row_level_security"),
        (_ARRAY, "array_or_user_type"),
        (_GENERATED_ALWAYS, "generated_always"),
    )
    for pattern, kind in checks:
        if pattern.search(body):
            scan.add(kind, table)
    if _has_unsupported_sequence_options(body):
        scan.add("sequence_unsupported", table)


def _read_sequence(sequence: str, body: str, scan: _Scan) -> None:
    if _has_unsupported_sequence_options(body):
        scan.add("sequence_unsupported", sequence)
    owner = _OWNED_BY.search(body)
    if owner is not None:
        scan.sequence_owned[sequence] = owner.group(1).upper() != "NONE"
    else:
        scan.sequence_owned.setdefault(sequence, False)


def _has_unsupported_sequence_options(body: str) -> bool:
    increments = [int(value) for value in _INCREMENT.findall(body)]
    return any(value != 1 for value in increments) or bool(_CYCLE.search(body))


def _has_unsupported_column(body: str) -> bool:
    return any(
        column_type not in _SUPPORTED_COLUMN_TYPES
        for column_type in _column_types(body)
    )


def _column_types(body: str) -> list[str]:
    types = []
    for element in _top_level_elements(body):
        first_word = element.split(" ", 1)[0].upper()
        if first_word in _TABLE_ELEMENT_KEYWORDS:
            continue
        name = re.match(r'"[^"]+"|\S+', element)
        if name is None:
            continue
        definition = element[name.end() :]
        definition = _COLUMN_CONSTRAINT.split(definition, maxsplit=1)[0]
        definition = re.sub(r"\([^)]*\)", " ", definition)
        definition = definition.lower().replace("pg_catalog.", "")
        types.append(" ".join(definition.replace('"', "").split()))
    return types


def _top_level_elements(body: str) -> list[str]:
    # CREATE TABLE 뒤 첫 괄호 안을 깊이 1 의 쉼표로 나눈다. 괄호가 없으면
    # (PARTITION OF 등) 열이 없다.
    start = body.find("(")
    if start < 0:
        return []
    elements, depth, current = [], 0, []
    for char in body[start + 1 :]:
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            elements.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    elements.append("".join(current).strip())
    return [element for element in elements if element]


def _normalize(name: str) -> str:
    parts = [part.strip('"').lower() for part in name.split(".")]
    if len(parts) == 1:
        parts.insert(0, "public")
    return ".".join(parts)
