import base64
import math
import re
from urllib.parse import urlsplit

ATTRIBUTE_KINDS = ("stringValue", "intValue", "doubleValue", "boolValue")
HTTP_STATUS_KEYS = ("http.response.status_code", "http.status_code")


def attributes(items) -> dict:
    result = {}
    for item in items or []:
        value = item.get("value", {})
        for kind in ATTRIBUTE_KINDS:
            if kind in value:
                result[item["key"]] = value[kind]
                break
    return result


def identifier(value, size: int = 8) -> str | None:
    if not value:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{" + str(size * 2) + r"}", value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
        return decoded.hex() if len(decoded) == size else None
    except (ValueError, TypeError):
        return None


def number(value, default=None):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError):
        return default


def path_only(value) -> str | None:
    return (urlsplit(str(value)).path or "/")[:500] if value else None


def http_status(tags: dict) -> int | None:
    code = number(tags.get(HTTP_STATUS_KEYS[0], tags.get(HTTP_STATUS_KEYS[1])))
    return int(code) if code is not None else None


def span_set_status(row: dict) -> int | None:
    codes = []
    for span_set in row.get("spanSets") or [row.get("spanSet", {})]:
        for span in span_set.get("spans", []):
            code = http_status(attributes(span.get("attributes")))
            if code is not None:
                codes.append(code)
    return max(codes, default=None)
