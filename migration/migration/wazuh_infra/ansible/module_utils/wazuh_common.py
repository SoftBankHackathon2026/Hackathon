"""Remote primitives. Never return service credentials or API response bodies."""

import base64
import json
import os
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

STATE = Path("/var/lib/oneport-wazuh-infra")
SERVICES = ("wazuh-indexer", "wazuh-manager", "filebeat", "wazuh-dashboard")


class WazuhError(Exception):
    pass


def check(condition, code):
    if not condition:
        raise WazuhError(code)


def no_symlinks(path):
    p = Path(path)
    check(p.is_absolute() and ".." not in p.parts, "STORAGE_PATH_INVALID")
    for ancestor in (p, *p.parents):
        check(not ancestor.is_symlink(), "SYMLINK_PATH_REFUSED")
    return p


def atomic(path, content, mode=0o600):
    p = no_symlinks(path)
    data = content.encode() if isinstance(content, str) else content
    if p.exists() and p.read_bytes() == data:
        os.chmod(p, mode)
        return False
    fd, tmp = tempfile.mkstemp(dir=p.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def save(path, value):
    return atomic(path, json.dumps(value, sort_keys=True))


def read(path):
    return json.loads(no_symlinks(path).read_text())


def storage_identity(path):
    p = no_symlinks(path)
    while not p.exists():
        p = p.parent
    if Path("/usr/bin/findmnt").exists():
        value = command(["/usr/bin/findmnt", "-n", "-o", "UUID", "-T", str(p)])
        if value:
            return "uuid:" + value
    return "device:" + str(p.stat().st_dev)


def command(argv, data=None, timeout=60, env=None):
    p = subprocess.run(
        argv, input=data, text=True, capture_output=True, timeout=timeout, env=env
    )
    check(p.returncode == 0, "HOST_COMMAND_FAILED")
    return p.stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(
    url,
    ca,
    method="GET",
    body=None,
    auth=None,
    cert=None,
    bearer=None,
    extra_headers=None,
):
    context = ssl.create_default_context(cafile=str(ca))
    if cert:
        context.load_cert_chain(str(cert[0]), str(cert[1]))
    headers = {"Content-Type": "application/json"}
    headers.update(extra_headers or {})
    if auth:
        headers["Authorization"] = (
            "Basic " + base64.b64encode((auth[0] + ":" + auth[1]).encode()).decode()
        )
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(
        NoRedirect(),
        urllib.request.HTTPSHandler(context=context),
        urllib.request.ProxyHandler({}),
    )
    try:
        with opener.open(req, timeout=15) as response:
            raw = response.read(8 * 1024 * 1024)
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, {}
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (OSError, urllib.error.URLError):
        raise WazuhError("TLS_OR_CONNECTION_FAILED") from None


def marker(c, digest=None):
    p = STATE / "installation.json"
    check(p.is_file(), "MANAGED_INSTALLATION_REQUIRED")
    m = read(p)
    check(
        m.get("installation_id") == c["target"]["installation_id"],
        "INSTALLATION_OWNER_MISMATCH",
    )
    check(
        m.get("machine_id") == Path("/etc/machine-id").read_text().strip(),
        "MACHINE_ID_MISMATCH",
    )
    if digest:
        check(m.get("digest") == digest, "CONFIGURATION_CHANGE_REQUIRES_NEW_PLAN")
    return m
