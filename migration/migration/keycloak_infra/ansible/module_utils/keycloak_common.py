"""Remote-safe probes. Never return raw process arguments or configuration files."""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import socket
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOTS = (
    "/opt/oneport-keycloak",
    "/etc/oneport-keycloak",
    "/var/lib/oneport-keycloak",
    "/var/lib/oneport-keycloak-postgres",
    "/etc/postgresql/18/oneport-keycloak",
    "/etc/apt/sources.list.d/oneport-keycloak-pgdg.list",
)
MANIFEST = "/var/lib/oneport-keycloak/install.json"


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return ""


def command(argv, **kwargs):
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=20, check=False, **kwargs
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def keycloak_hint(value):
    return bool(
        re.search(
            r"keycloak|org\.keycloak|(?:^|[/\s])kc\.sh(?:\s|$)", value, re.IGNORECASE
        )
    )


def unit_candidates(output):
    candidates = []
    for block in output.split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        exec_start = fields.get("ExecStart", "")
        server_exec = re.search(
            r"/bin/kc\.sh|org\.keycloak|/keycloak(?:-\d[^/\s]*)?/",
            exec_start,
            re.IGNORECASE,
        )
        companion_unit = re.search(
            r"(?:^|[-_.])(?:postgres(?:ql)?|mysql|mariadb|operator)(?:[-_.]|$)",
            fields.get("Id", ""),
            re.IGNORECASE,
        )
        if companion_unit and not server_exec:
            continue
        if fields.get("Id") and keycloak_hint(
            fields.get("Id", "") + " " + fields.get("ExecStart", "")
        ):
            executable = fields.get("ExecStart", "")
            public = {}
            paths = re.findall(r"--config-file[=\s]+([^\s;\"']+)", executable)
            paths.extend(re.findall(r"path=([^ ;]+?)/bin/kc\.sh", executable))
            for path in paths:
                path = path if path.endswith(".conf") else path + "/conf/keycloak.conf"
                for line in read(path).splitlines():
                    if "=" in line:
                        key, value = line.strip().split("=", 1)
                        if key in (
                            "hostname",
                            "https-port",
                            "http-port",
                            "http-enabled",
                        ):
                            public[key] = value.strip()
            for key in ("hostname", "https-port", "http-port", "http-enabled"):
                match = re.search(r"--" + key + r"[=\s]+([^\s;\"']+)", executable)
                if match:
                    public[key] = match.group(1)
            endpoints = []
            if public.get("hostname", "").startswith(("https://", "http://")):
                try:
                    parsed = urlsplit(public["hostname"])
                    if (
                        parsed.hostname
                        and not parsed.username
                        and not parsed.password
                        and not parsed.query
                        and not parsed.fragment
                    ):
                        endpoints.append(
                            {
                                "base_url": public["hostname"],
                                "verified": False,
                                "source": "host_configuration",
                            }
                        )
                except ValueError:
                    pass
            version = re.search(r"/keycloak-(\d+\.\d+\.\d+)(?:/|\s)", executable)
            candidates.append(
                {
                    "deployment_type": "host_service",
                    "service": fields["Id"],
                    "state": fields.get("ActiveState", "unknown"),
                    "version": None,
                    "evidence": ["systemd_unit"],
                    "endpoints": endpoints,
                    "version_hint": version.group(1) if version else None,
                    "next_actions": []
                    if endpoints
                    else ["provide_endpoint_for_inspection"],
                }
            )
    return candidates


def normalize_machine(value):
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def same_machine(node, host):
    pairs = [
        (normalize_machine(node.get(key)), normalize_machine(host.get(key)))
        for key in ("machine_id", "system_uuid")
        if node.get(key) and host.get(key)
    ]
    return bool(pairs) and all(a == b for a, b in pairs)


def port_free(port):
    try:
        with socket.socket() as s:
            s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


def host_info():
    osinfo = {}
    for line in read("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osinfo[k] = v.strip('"')
    manifest = None
    try:
        obj = json.loads(read(MANIFEST))
        manifest = {
            key: obj[key]
            for key in (
                "install_id",
                "node_uid",
                "base_url",
                "https_port",
                "db_port",
                "keycloak_version",
                "java_version",
                "postgres_major",
            )
        }
    except (ValueError, KeyError, TypeError):
        pass
    candidates, coverage = [], "unknown"
    if shutil.which("systemctl"):
        units = command(
            [
                "systemctl",
                "list-unit-files",
                "--type=service",
                "--no-legend",
                "--no-pager",
            ]
        )
        if units and units.returncode == 0:
            names = [
                line.split()[0]
                for line in units.stdout.splitlines()
                if line.strip()
                and re.fullmatch(r"[A-Za-z0-9_.@:\\-]+\.service", line.split()[0])
            ]
            coverage = "complete"
            for start in range(0, len(names), 64):
                result = command(
                    [
                        "systemctl",
                        "show",
                        "--no-pager",
                        "--property=Id,ExecStart,ActiveState",
                        "--",
                        *names[start : start + 64],
                    ]
                )
                if not result or result.returncode:
                    coverage = "unknown"
                else:
                    candidates.extend(unit_candidates(result.stdout))
    # Catch unmanaged native processes, while excluding Kubernetes/container
    # cgroups already covered by API discovery. Arguments never leave this host.
    for process in Path("/proc").glob("[0-9]*"):
        args = read(process / "cmdline").replace("\x00", " ")
        cgroup = read(process / "cgroup")
        native = re.search(
            r"org\.keycloak|(?:^|[/\s])kc\.sh(?:\s|$)|/keycloak(?:-\d[^/\s]*)?/",
            args,
            re.IGNORECASE,
        )
        if not native or re.search(r"kubepods|containerd|docker|libpod", cgroup):
            continue
        if any(c.get("service") and c["service"] in cgroup for c in candidates):
            continue
        candidates.append(
            {
                "deployment_type": "host_process",
                "service": "process:" + process.name,
                "state": "running",
                "version": None,
                "evidence": ["native_process"],
                "endpoints": [],
                "next_actions": ["provide_endpoint_for_inspection"],
            }
        )
    memory = dict(
        line.split(":", 1) for line in read("/proc/meminfo").splitlines() if ":" in line
    )
    available = int(memory.get("MemAvailable", "0 kB").split()[0]) // 1024
    free = min(
        shutil.disk_usage(p).free // 1024 // 1024 for p in ("/opt", "/var/lib", "/etc")
    )
    managed_active = {}
    for unit in ("oneport-keycloak", "oneport-keycloak-postgres"):
        r = command(["systemctl", "is-active", unit])
        managed_active[unit] = bool(r and r.returncode == 0)
    existing_accounts = []
    for account in ("oneport-keycloak", "oneport-kc-db"):
        try:
            existing_accounts.append(
                {"name": account, "uid": pwd.getpwnam(account).pw_uid}
            )
        except KeyError:
            pass
    repository_files = [
        Path("/etc/apt/sources.list"),
        *Path("/etc/apt/sources.list.d").glob("*.list"),
        *Path("/etc/apt/sources.list.d").glob("*.sources"),
    ]
    pgdg_repository_present = any(
        re.search(r"(?m)^\s*(?:deb\s|URIs:).*https?://apt\.postgresql\.org/", read(p))
        and not re.search(r"(?im)^Enabled:\s*no\s*$", read(p))
        for p in repository_files
    )
    return {
        "machine_id": read("/etc/machine-id"),
        "system_uuid": read("/sys/class/dmi/id/product_uuid"),
        "os_id": osinfo.get("ID"),
        "os_version": osinfo.get("VERSION_ID"),
        "codename": osinfo.get("VERSION_CODENAME"),
        "architecture": os.uname().machine,
        "systemd": Path("/run/systemd/system").is_dir(),
        "memory_available_mb": available,
        "free_disk_mb": free,
        "coverage": coverage,
        "detection_method": "systemd_unit_name_and_exec_path_heuristic",
        "candidates": candidates,
        "managed_manifest": manifest,
        "managed_active": managed_active,
        "existing_accounts": existing_accounts,
        "pgdg_repository_present": bool(pgdg_repository_present),
        "existing_roots": [p for p in ROOTS if Path(p).exists()],
        "unit_conflict": any(
            Path(f"/etc/systemd/system/{u}.service").exists() for u in managed_active
        ),
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url, ca_file=None):
    context = ssl.create_default_context(cafile=ca_file)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect,
        urllib.request.HTTPSHandler(context=context),
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener.open(req, timeout=10) as response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError("response too large")
        obj = json.loads(data)
        if not isinstance(obj, dict):
            raise ValueError("invalid response shape")  # noqa: TRY004 - wire format failure
        return obj


def endpoint_info(base_url, realm="master", ca_file=None, management_url=None):
    issuer = base_url.rstrip("/") + "/realms/" + quote(realm, safe="")
    checks = []
    connection = {
        "base_url": base_url,
        "realm": realm,
        "issuer": issuer,
        "version": None,
        "admin_access": "not_checked",
    }
    try:
        metadata = get_json(issuer + "/.well-known/openid-configuration", ca_file)
        expected = issuer + "/protocol/openid-connect/"
        valid = (
            metadata.get("issuer") == issuer
            and metadata.get("jwks_uri") == expected + "certs"
            and metadata.get("token_endpoint") == expected + "token"
        )
        checks.append(
            {
                "name": "oidc_discovery",
                "status": "pass" if valid else "fail",
                "reason_code": "ISSUER_OR_ENDPOINT_MISMATCH"
                if not valid
                else "DISCOVERY_MATCHED",
            }
        )
        if valid:
            jwks = get_json(expected + "certs", ca_file)
            keys = jwks.get("keys")
            checks.append(
                {
                    "name": "signing_keys",
                    "status": "pass"
                    if isinstance(keys, list)
                    and any(
                        isinstance(k, dict) and k.get("kty") and k.get("kid")
                        for k in keys
                    )
                    else "fail",
                }
            )
    except (OSError, ValueError, urllib.error.URLError):
        checks.append(
            {
                "name": "oidc_discovery",
                "status": "fail",
                "reason_code": "ENDPOINT_UNREACHABLE_OR_INVALID",
            }
        )
    if management_url:
        try:
            ready = get_json(management_url.rstrip("/") + "/health/ready", ca_file)
            checks.append(
                {
                    "name": "readiness",
                    "status": "pass" if ready.get("status") == "UP" else "fail",
                }
            )
        except (OSError, ValueError, urllib.error.URLError):
            checks.append(
                {
                    "name": "readiness",
                    "status": "fail",
                    "reason_code": "READINESS_UNAVAILABLE",
                }
            )
    else:
        checks.append(
            {
                "name": "readiness",
                "status": "unknown",
                "reason_code": "MANAGEMENT_ENDPOINT_NOT_PROVIDED",
            }
        )
    if base_url.startswith("http:"):
        checks.append(
            {
                "name": "transport_encryption",
                "status": "fail",
                "reason_code": "PLAINTEXT_HTTP",
            }
        )
    return {"connection": connection, "checks": checks}
