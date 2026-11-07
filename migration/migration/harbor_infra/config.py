"""Strict reference-only request contract, independent of pending IAM/secret PRs."""

from __future__ import annotations

import copy
import ipaddress
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
    "trust",
    "verify",
    "handoff",
)
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}\Z")


class ConfigError(ValueError):
    """Errors report field names only, never submitted values."""


def require(ok, field):
    if not ok:
        raise ConfigError("invalid or missing field: " + field)


def keys(value, fields, field):
    require(isinstance(value, dict) and not set(value) - set(fields.split()), field)


def ident(value, field, pattern=ID):
    require(isinstance(value, str) and bool(pattern.fullmatch(value)), field)
    return value


def path(value, field, exists=True):
    require(
        isinstance(value, str)
        and value.startswith("/")
        and not re.search(r"[\s\x00]", value),
        field,
    )
    p = Path(value)
    require(".." not in p.parts and not p.is_symlink(), field)
    require(not exists or p.is_file(), field)
    return value


def secret_file(value):
    try:
        fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            st = os.fstat(f.fileno())
            require(
                stat.S_ISREG(st.st_mode)
                and st.st_uid == os.getuid()
                and not st.st_mode & 0o077,
                "secret_ref.permissions",
            )
            data = f.read(65537).strip()
        require(0 < len(data) <= 65536, "secret_ref.length")
        require(
            not any(t in data for t in ("{{", "{%", "{#")), "secret_ref.template_syntax"
        )
        return data
    except OSError:
        raise ConfigError("cannot read protected secret reference") from None


def bootstrap_secret(value, field):
    # Harbor's generated dotenv files must not reinterpret dollar signs or newlines.
    require(
        bool(re.fullmatch(r"[A-Za-z0-9_@%+=.,:/!-]{24,256}", value)), field + ".format"
    )
    return value


def url(value, field):
    try:
        u = urlsplit(value)
        valid = (
            u.scheme == "https"
            and u.hostname
            and not (u.username or u.password or u.query or u.fragment)
            and u.path in ("", "/")
            and (u.port is None or 0 < u.port <= 65535)
        )
        require(valid and not re.search(r"[\s\\{}\x00]", value), field)
        # Restrict hostnames used in certificates and generated configurations.
        try:
            ipaddress.ip_address(u.hostname)
        except ValueError:
            require(bool(re.fullmatch(r"[A-Za-z0-9.-]+", u.hostname)), field)
        return value.rstrip("/")
    except (TypeError, ValueError, AttributeError):
        raise ConfigError("invalid or missing field: " + field) from None


def integer(value, field, minimum, maximum):
    require(type(value) is int and minimum <= value <= maximum, field)
    return value


def validate(raw, operation):
    require(operation in OPERATIONS, "operation")
    keys(
        raw,
        "schema_version request_id organization_id cluster nodes target endpoint install tls keycloak projects trust handoff verification plan_digest timeout_seconds",
        "config",
    )

    def safe_strings(value):
        if isinstance(value, str):
            require(not any(t in value for t in ("{{", "{%", "{#")), "template_syntax")
        elif isinstance(value, dict):
            for v in value.values():
                safe_strings(v)
        elif isinstance(value, list):
            for v in value:
                safe_strings(v)

    safe_strings(raw)
    c = copy.deepcopy(raw)
    require(
        type(c.get("schema_version")) is int and c["schema_version"] == 1,
        "schema_version",
    )
    ident(c.get("request_id"), "request_id")
    ident(c.get("organization_id"), "organization_id")
    integer(c.setdefault("timeout_seconds", 1800), "timeout_seconds", 10, 7200)
    if operation != "discover":
        ep = c.get("endpoint")
        keys(ep, "url ca_ref admin_username admin_password_ref", "endpoint")
        ep["url"] = url(ep.get("url"), "endpoint.url")
        if ep.get("ca_ref"):
            path(ep["ca_ref"], "endpoint.ca_ref")
        ident(ep.setdefault("admin_username", "admin"), "endpoint.admin_username")
        if ep.get("admin_password_ref"):
            path(ep["admin_password_ref"], "endpoint.admin_password_ref")
    if operation not in ("discover", "inspect"):
        target = c.get("target")
        keys(target, "mode installation_id node_name", "target")
        require(target.get("mode") in ("new", "reuse"), "target.mode")
        ident(
            target.get("installation_id"),
            "target.installation_id",
            re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z"),
        )
        if target["mode"] == "new":
            ident(target.get("node_name"), "target.node_name", NAME)
        require(operation != "install" or target["mode"] == "new", "install.new_only")
    nodes = c.setdefault("nodes", [])
    require(isinstance(nodes, list) and len(nodes) <= 128, "nodes")
    seen = set()
    for n in nodes:
        keys(
            n,
            "name uid ssh_host ssh_port ssh_user private_key_ref known_hosts_ref become_password_ref",
            "nodes[]",
        )
        name = ident(n.get("name"), "nodes[].name", NAME)
        require(name not in seen, "nodes[].unique")
        seen.add(name)
        ident(n.get("uid"), "nodes[].uid")
        require(
            isinstance(n.get("ssh_host"), str)
            and bool(re.fullmatch(r"[A-Za-z0-9_.:-]+", n["ssh_host"]))
            and not n["ssh_host"].startswith("-"),
            "nodes[].ssh_host",
        )
        ident(n.get("ssh_user"), "nodes[].ssh_user")
        integer(n.setdefault("ssh_port", 22), "nodes[].ssh_port", 1, 65535)
        for key in ("private_key_ref", "known_hosts_ref"):
            path(n.get(key), "nodes[]." + key)
        if n.get("become_password_ref"):
            path(n["become_password_ref"], "nodes[].become_password_ref")
    needs_cluster = (
        operation == "discover"
        or operation == "trust"
        or (
            operation in ("preflight", "install", "verify")
            and c["target"]["mode"] == "new"
        )
    )
    if needs_cluster or c.get("cluster"):
        cl = c.get("cluster")
        keys(cl, "id kubeconfig_ref context namespaces", "cluster")
        ident(cl.get("id"), "cluster.id")
        path(cl.get("kubeconfig_ref"), "cluster.kubeconfig_ref")
        require(
            isinstance(cl.get("context"), str)
            and bool(cl["context"])
            and not re.search(r"[\x00\r\n]", cl["context"]),
            "cluster.context",
        )
        ns = cl.setdefault("namespaces", [])
        require(isinstance(ns, list) and len(ns) <= 128, "cluster.namespaces")
        for v in ns:
            ident(v, "cluster.namespaces[]", NAME)
    if (operation in ("preflight", "install", "verify") or c.get("install")) and c.get(
        "target", {}
    ).get("mode") == "new":
        require(c["target"]["node_name"] in seen, "target.node_name")
        ins = c.get("install")
        keys(
            ins,
            "data_path mount_path capacity_gb install_runtime database_password_ref",
            "install",
        )
        path(ins.get("data_path"), "install.data_path", False)
        path(ins.get("mount_path"), "install.mount_path", False)
        require(
            ins["data_path"] not in ("/", "/data", "/var", "/var/lib", "/etc", "/opt")
            and Path(ins["data_path"]).is_relative_to(ins["mount_path"]),
            "install.data_path.dedicated",
        )
        require(
            bool(re.fullmatch(r"/[A-Za-z0-9_./-]+", ins["data_path"])),
            "install.data_path.characters",
        )
        integer(ins.get("capacity_gb"), "install.capacity_gb", 40, 1000000)
        require(
            type(ins.setdefault("install_runtime", True)) is bool,
            "install.install_runtime",
        )
        path(ins.get("database_password_ref"), "install.database_password_ref")
        tls = c.get("tls")
        keys(tls, "mode certificate_ref private_key_ref ca_ref", "tls")
        require(tls.get("mode") in ("provided", "self_signed"), "tls.mode")
        if tls["mode"] == "provided":
            for key in ("certificate_ref", "private_key_ref", "ca_ref"):
                path(tls.get(key), "tls." + key)
    if operation in ("install", "configure"):
        require(
            bool(c["endpoint"].get("admin_password_ref")), "endpoint.admin_password_ref"
        )
    if operation in ("configure", "verify") or c.get("keycloak"):
        kc = c.get("keycloak")
        keys(
            kc,
            "issuer_url ca_ref client_id client_secret_ref username_claim groups_claim scope admin_group manage_client admin_token_ref",
            "keycloak",
        )
        # An issuer includes its realm path, unlike a registry endpoint.
        issuer = kc.get("issuer_url", "")
        try:
            parsed = urlsplit(issuer)
            url(parsed._replace(path="").geturl(), "keycloak.issuer_url")
            require(not re.search(r"[\s\x00]", parsed.path), "keycloak.issuer_url")
        except (ValueError, AttributeError):
            raise ConfigError("invalid or missing field: keycloak.issuer_url") from None
        require(
            type(kc.setdefault("manage_client", False)) is bool,
            "keycloak.manage_client",
        )
        if kc["manage_client"]:
            path(kc.get("admin_token_ref"), "keycloak.admin_token_ref")
        ident(kc.get("client_id"), "keycloak.client_id")
        path(kc.get("client_secret_ref"), "keycloak.client_secret_ref")
        if kc.get("ca_ref"):
            path(kc["ca_ref"], "keycloak.ca_ref")
        for key, default in [
            ("username_claim", "preferred_username"),
            ("groups_claim", "groups"),
        ]:
            ident(kc.setdefault(key, default), "keycloak." + key)
        require(
            isinstance(kc.setdefault("scope", "openid,profile,email"), str)
            and bool(re.fullmatch(r"[A-Za-z0-9_,.-]+", kc["scope"])),
            "keycloak.scope",
        )
        require(
            isinstance(kc.setdefault("admin_group", ""), str)
            and not re.search(r"[\r\n\x00]", kc["admin_group"]),
            "keycloak.admin_group",
        )
    if operation in ("configure", "verify") or c.get("projects"):
        projects = c.get("projects")
        require(isinstance(projects, list) and 0 < len(projects) <= 100, "projects")
        names = set()
        for project in projects:
            keys(
                project,
                "name adopt_existing writer_days reader_days groups",
                "projects[]",
            )
            name = ident(project.get("name"), "projects[].name", NAME)
            require(name not in names, "projects[].unique")
            names.add(name)
            require(
                type(project.setdefault("adopt_existing", False)) is bool,
                "projects[].adopt_existing",
            )
            for key in ("writer_days", "reader_days"):
                integer(project.get(key), "projects[]." + key, 1, 365)
            groups = project.setdefault("groups", [])
            require(isinstance(groups, list), "projects[].groups")
            for group in groups:
                keys(group, "name role_id", "projects[].groups[]")
                require(
                    isinstance(group.get("name"), str)
                    and bool(group["name"])
                    and not re.search(r"[\r\n\x00]", group["name"]),
                    "projects[].groups[].name",
                )
                integer(group.get("role_id"), "projects[].groups[].role_id", 1, 4)
    trust = c.setdefault("trust", {"targets": [], "restart_approved": []})
    keys(trust, "targets restart_approved", "trust")
    for key in ("targets", "restart_approved"):
        values = trust.setdefault(key, [])
        require(
            isinstance(values, list)
            and len(set(values)) == len(values)
            and set(values) <= seen,
            "trust." + key,
        )
    require(
        set(trust["restart_approved"]) <= set(trust["targets"]),
        "trust.restart_approved",
    )
    if operation == "trust":
        require(bool(trust["targets"]), "trust.targets")
    if operation in ("install", "configure", "handoff", "verify") or c.get("handoff"):
        ho = c.get("handoff")
        keys(ho, "directory action receipt_digest ttl_seconds", "handoff")
        path(ho.get("directory"), "handoff.directory", False)
        integer(ho.setdefault("ttl_seconds", 3600), "handoff.ttl_seconds", 60, 86400)
        if operation == "handoff":
            require(ho.get("action") in ("prepare", "ack", "cleanup"), "handoff.action")
            if ho["action"] == "ack":
                require(
                    bool(re.fullmatch(r"[a-f0-9]{64}", ho.get("receipt_digest", ""))),
                    "handoff.receipt_digest",
                )
    ver = c.setdefault("verification", {})
    keys(ver, "restart_harbor image_tests runtime_pull test_namespace", "verification")
    require(
        type(ver.setdefault("restart_harbor", False)) is bool,
        "verification.restart_harbor",
    )
    for key in ("image_tests", "runtime_pull"):
        require(type(ver.setdefault(key, False)) is bool, "verification." + key)
    if ver["runtime_pull"]:
        require(
            ver["image_tests"] and bool(trust["targets"]), "verification.runtime_pull"
        )
        ident(ver.get("test_namespace"), "verification.test_namespace", NAME)
    if operation in ("install", "configure", "trust"):
        require(
            bool(re.fullmatch(r"[a-f0-9]{64}", c.get("plan_digest", ""))), "plan_digest"
        )
    return c


def load(pathname, operation):
    try:
        return validate(yaml.safe_load(Path(pathname).read_text()), operation)
    except (OSError, yaml.YAMLError):
        raise ConfigError("cannot read configuration") from None
