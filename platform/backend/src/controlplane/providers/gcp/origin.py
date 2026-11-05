from urllib.parse import urlsplit


def is_registered_origin(url: str) -> bool:
    parts = urlsplit(url)
    return not (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.path
        or parts.query
        or parts.fragment
    )
