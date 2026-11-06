"""Strict, reference-only platform input. Never include submitted values in errors."""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ASSETS = Path(__file__).parent / "ansible"
OPERATIONS = (
    "discover",
    "inspect",
    "preflight",
    "install",
    "configure",
    "verify",
    "handoff",
    "finalize",
    "issue-migration-token",
)
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
LOGICAL = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")


class ConfigError(ValueError):
    pass


def require(ok, field):
    if not ok:
        raise ConfigError("invalid or missing field: " + field)


def keys(value, allowed, field):
    require(isinstance(value, dict) and not set(value) - set(allowed.split()), field)


def text(value, field, pattern=ID):
    require(isinstance(value, str) and pattern.fullmatch(value), field)
    return value


def integer(value, field, minimum=1, maximum=65535):
    require(type(value) is int and minimum <= value <= maximum, field)
    return value


def path(value, field, exists=True):
    require(
        isinstance(value, str)
        and value.startswith("/")
        and not any(c in value for c in "\n\r\x00"),
        field,
    )
    p = Path(value)
    require(not p.is_symlink() and (not exists or p.is_file()), field)
    return str(p)


def secret_file(value):
    """Open without following symlinks; credentials are never general config values."""
    fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and not info.st_mode & 0o077
            and info.st_uid == os.getuid(),
            "credential_ref.permissions",
        )
        data = stream.read(65537)
    require(0 < len(data) <= 65536, "credential_ref.length")
    return data.strip()


def endpoint(value, field):
    try:
        u = urlsplit(value)
        ok = (
            u.scheme == "https"
            and u.hostname
            and not u.username
            and not u.password
            and not u.query
            and not u.fragment
            and u.path in ("", "/")
            and (u.port or 443) > 0
        )
        require(
            ok
            and not any(c in value for c in '\\"{}\n\r\x00')
            and not any(c.isspace() for c in value),
            field,
        )
    except (ValueError, TypeError, AttributeError):
        raise ConfigError("invalid or missing field: " + field) from None
    return value.rstrip("/")


def validate(raw, operation):
    require(operation in OPERATIONS, "operation")
    keys(
        raw,
        "schema_version request_id cluster nodes target endpoint tls access keycloak applications handoff verification timeout_seconds",
        "config",
    )
    c = copy.deepcopy(raw)
    require(c.get("schema_version") == 1, "schema_version")
    text(c.get("request_id"), "request_id")
    integer(c.setdefault("timeout_seconds", 1800), "timeout_seconds", 10, 7200)
    cluster = c.get("cluster")
    keys(cluster, "id kubeconfig context", "cluster")
    text(cluster.get("id"), "cluster.id")
    path(cluster.get("kubeconfig"), "cluster.kubeconfig")
    require(
        isinstance(cluster.get("context"), str) and bool(cluster["context"]),
        "cluster.context",
    )
    nodes = c.setdefault("nodes", [])
    require(isinstance(nodes, list), "nodes")
    seen = set()
    for n in nodes:
        keys(
            n,
            "name uid ssh_host ssh_port ssh_user private_key_file known_hosts_file become_password_ref",
            "nodes[]",
        )
        text(n.get("name"), "nodes.name", NAME)
        require(n["name"] not in seen, "nodes.duplicate")
        seen.add(n["name"])
        require(
            isinstance(n.get("uid"), str)
            and re.fullmatch(r"[A-Za-z0-9-]{1,128}", n["uid"]),
            "nodes.uid",
        )
        require(
            isinstance(n.get("ssh_host"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", n["ssh_host"]),
            "nodes.ssh_host",
        )
        integer(n.setdefault("ssh_port", 22), "nodes.ssh_port")
        require(
            isinstance(n.get("ssh_user"), str)
            and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", n["ssh_user"]),
            "nodes.ssh_user",
        )
        for f in ("private_key_file", "known_hosts_file"):
            path(n.get(f), "nodes." + f)
        if "become_password_ref" in n:
            path(n["become_password_ref"], "nodes.become_password_ref")
    if operation == "discover":
        return c
    e = c.get("endpoint")
    keys(e, "url ca_file admin_token_ref product_confirmed", "endpoint")
    e["url"] = endpoint(e.get("url"), "endpoint.url")
    if "ca_file" in e:
        path(e["ca_file"], "endpoint.ca_file")
    if "admin_token_ref" in e:
        path(e["admin_token_ref"], "endpoint.admin_token_ref")
    require(
        type(e.setdefault("product_confirmed", False)) is bool,
        "endpoint.product_confirmed",
    )
    if operation == "inspect":
        return c
    t = c.get("target")
    keys(
        t,
        "installation_id node_name bind_address port min_memory_mb min_disk_mb allow_control_plane mode",
        "target",
    )
    require(t.setdefault("mode", "install") in ("install", "reuse"), "target.mode")
    if operation == "install":
        require(t["mode"] == "install", "target.mode")
    if t["mode"] == "reuse" and operation in (
        "configure",
        "verify",
        "issue-migration-token",
    ):
        require(
            e["product_confirmed"] and "admin_token_ref" in e and "ca_file" in e,
            "endpoint.reuse_credentials_and_identity",
        )
    text(t.get("installation_id"), "target.installation_id")
    require(t.get("node_name") in seen, "target.node_name")
    try:
        ipaddress.ip_address(t.get("bind_address"))
    except ValueError:
        raise ConfigError("invalid or missing field: target.bind_address") from None
    integer(t.setdefault("port", 8200), "target.port", 1024, 65534)
    require(t["port"] != 8201, "target.reserved_cluster_port")
    require((urlsplit(e["url"]).port or 443) == t["port"], "target.endpoint_port")
    for f, v in (("min_memory_mb", 1024), ("min_disk_mb", 2048)):
        integer(t.setdefault(f, v), "target." + f, 256, 1048576)
    require(
        type(t.setdefault("allow_control_plane", False)) is bool,
        "target.allow_control_plane",
    )
    tls = c.setdefault("tls", {"mode": "self_signed"})
    keys(tls, "mode certificate_ref private_key_ref ca_ref validity_days", "tls")
    require(tls.get("mode") in ("provided", "self_signed"), "tls.mode")
    integer(tls.setdefault("validity_days", 365), "tls.validity_days", 1, 825)
    if tls["mode"] == "provided":
        for f in ("certificate_ref", "private_key_ref", "ca_ref"):
            path(tls.get(f), "tls." + f)
    access = c.setdefault("access", {})
    keys(access, "mount prefix token_ttl_seconds secret_id_ttl_seconds", "access")
    for f, v in (("mount", "kv"), ("prefix", "migration")):
        text(access.setdefault(f, v), "access." + f, LOGICAL)
    require("/" not in access["mount"], "access.mount")
    integer(
        access.setdefault("token_ttl_seconds", 900),
        "access.token_ttl_seconds",
        60,
        3600,
    )
    integer(
        access.setdefault("secret_id_ttl_seconds", 300),
        "access.secret_id_ttl_seconds",
        60,
        900,
    )
    kc = c.get("keycloak")
    if kc:
        keys(
            kc,
            "issuer client_id client_secret_ref groups_claim allowed_groups redirect_uris ca_ref manage_client admin_url admin_token_ref",
            "keycloak",
        )
        issuer = kc.get("issuer", "")
        try:
            u = urlsplit(issuer)
            require(
                u.path.startswith("/realms/") and len(u.path.split("/")) == 3,
                "keycloak.issuer",
            )
            endpoint(f"https://{u.netloc}", "keycloak.issuer")
            require(
                u.scheme == "https" and not u.query and not u.fragment,
                "keycloak.issuer",
            )
        except ValueError:
            raise ConfigError("invalid or missing field: keycloak.issuer") from None
        text(kc.get("client_id"), "keycloak.client_id")
        text(kc.setdefault("groups_claim", "groups"), "keycloak.groups_claim")
        for f in ("client_secret_ref", "ca_ref"):
            path(kc.get(f), "keycloak." + f)
        require(
            type(kc.setdefault("manage_client", False)) is bool,
            "keycloak.manage_client",
        )
        if kc["manage_client"]:
            kc["admin_url"] = endpoint(kc.get("admin_url"), "keycloak.admin_url")
            path(kc.get("admin_token_ref"), "keycloak.admin_token_ref")
        require(
            isinstance(kc.get("allowed_groups"), list)
            and bool(kc["allowed_groups"])
            and all(
                isinstance(g, str) and re.fullmatch(r"/[A-Za-z0-9_/-]+", g)
                for g in kc["allowed_groups"]
            ),
            "keycloak.allowed_groups",
        )
        expected = (
            e["url"]
            + "/ui/vault/auth/oi-"
            + t["installation_id"]
            + "-operators/oidc/callback"
        )
        require(kc.get("redirect_uris") == [expected], "keycloak.redirect_uris")
    apps = c.setdefault("applications", [])
    require(isinstance(apps, list), "applications")
    appnames = set()
    for a in apps:
        keys(
            a,
            "name namespace service_account prefix issuer discovery_url ca_ref audience token_ttl_seconds probe_jwt_ref",
            "applications[]",
        )
        text(a.get("name"), "applications.name")
        require(a["name"] not in appnames, "applications.duplicate")
        appnames.add(a["name"])
        for f in ("namespace", "service_account"):
            text(a.get(f), "applications." + f, NAME)
        text(a.get("prefix"), "applications.prefix", LOGICAL)
        # Separate mounts per issuer/application; no cross-cluster key mixing.
        a["issuer"] = endpoint(a.get("issuer"), "applications.issuer")
        a["discovery_url"] = endpoint(
            a.get("discovery_url", a["issuer"]), "applications.discovery_url"
        )
        text(a.setdefault("audience", "openbao"), "applications.audience")
        path(a.get("ca_ref"), "applications.ca_ref")
        if "probe_jwt_ref" in a:
            path(a["probe_jwt_ref"], "applications.probe_jwt_ref")
        integer(
            a.setdefault("token_ttl_seconds", 300),
            "applications.token_ttl_seconds",
            60,
            600,
        )
    handoff = c.setdefault("handoff", {})
    keys(
        handoff,
        "action directory receipt_path owner_id download_confirmed ttl_seconds",
        "handoff",
    )
    if operation == "handoff":
        require(
            handoff.get("action") in ("prepare", "ack", "cleanup"), "handoff.action"
        )
        path(handoff.get("directory"), "handoff.directory", exists=False)
        path(handoff.get("receipt_path"), "handoff.receipt_path", exists=False)
        text(handoff.get("owner_id"), "handoff.owner_id")
        integer(handoff.setdefault("ttl_seconds", 900), "handoff.ttl_seconds", 60, 3600)
        if handoff["action"] == "ack":
            require(
                handoff.get("download_confirmed") is True, "handoff.download_confirmed"
            )
    verification = c.setdefault("verification", {})
    keys(verification, "restart operator_token_ref", "verification")
    require(
        type(verification.setdefault("restart", False)) is bool, "verification.restart"
    )
    if "operator_token_ref" in verification:
        path(verification["operator_token_ref"], "verification.operator_token_ref")
    if operation == "finalize":
        require("operator_token_ref" in verification, "verification.operator_token_ref")
    if t["mode"] == "reuse":
        require(
            operation not in ("handoff", "finalize") and not verification["restart"],
            "target.reuse_non_disruptive",
        )
    if operation == "issue-migration-token":
        path(handoff.get("directory"), "handoff.directory", exists=False)
    return c


def load(path_, operation):
    try:
        return validate(yaml.safe_load(Path(path_).read_text()), operation)
    except ConfigError:
        raise
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        raise ConfigError("configuration could not be read") from None


def public_copy(config):
    """Useful for plans; references stay references, not resolved credentials."""
    return json.loads(json.dumps(config))
