import ipaddress

PRIVATE_SUFFIXES = (".local", ".internal", ".svc", ".lan", ".ts.net", ".home.arpa")
DEFAULT_PORTS = {"http": 80, "https": 443}


def exposure_of(host: str) -> str:
    try:
        return "public" if ipaddress.ip_address(host).is_global else "private"
    except ValueError:
        pass
    internal_elb = host.startswith("internal-") and ".elb." in host
    if internal_elb or "." not in host or host.endswith(PRIVATE_SUFFIXES):
        return "private"
    return "public"


def port_scheme(port: int, name: str) -> str:
    lowered = name.lower()
    if port == 443 or "https" in lowered:
        return "https"
    if port in (80, 8080) or "http" in lowered:
        return "http"
    return "tcp"


def url_of(scheme: str, host: str, port: int | None = None, path: str = "") -> str:
    shown = "" if port is None or DEFAULT_PORTS.get(scheme) == port else f":{port}"
    return f"{scheme}://{host}{shown}{path}"
