import re

KEY_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
KEY_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")
MASK = "●●●"

_SECRET_KEY = (
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|client[_-]?secret|credential|session[_-]?id|cookie)[A-Za-z0-9_.-]{0,40}"
)

_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:bearer|basic|token)?\s*[^\s,;\"']+"),
        rf"\1{MASK}",
    ),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {MASK}"),
    (
        re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]{0,15}://[^\s:/@\"']{1,128}:)[^\s@\"']{1,256}(@)"),
        rf"\1{MASK}\2",
    ),
    (
        re.compile(rf"(?i)(\"?{_SECRET_KEY}\"?\s*[:=]\s*\"?)([^\s\"',;&}}]+)"),
        rf"\1{MASK}",
    ),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MASK),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"), MASK),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), MASK),
    (re.compile(r"\b(?:hvs|hvb|s)\.[A-Za-z0-9]{24,}"), MASK),
]


def redact(line: str) -> tuple[str, int]:
    count = 0
    for pattern, replacement in _RULES:
        line, found = pattern.subn(replacement, line)
        count += found
    return line, count


def redact_lines(lines: list[str]) -> tuple[list[str], int]:
    result, total, in_key = [], 0, False
    for line in lines:
        if in_key or KEY_BEGIN.search(line):
            begin = KEY_BEGIN.search(line)
            prefix = line[: begin.start()] if begin and not in_key else ""
            in_key = not KEY_END.search(line)
            result.append(prefix + MASK)
            total += 1
            continue
        masked, count = redact(line)
        result.append(masked)
        total += count
    return result, total
