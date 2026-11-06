"""Strict platform input contract. Credentials are environment/file references only."""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

OPERATIONS = ("discover", "inspect", "preflight", "install", "verify")
ASSETS = Path(__file__).parent / "ansible"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """Error messages contain field names, never supplied values."""


def require(condition, field):
    if not condition:
        raise ConfigError(f"invalid or missing field: {field}")


def exact_keys(value, allowed, field):
    require(isinstance(value, dict), field)
    require(not set(value).difference(allowed), field + ".unknown_fields")


def text(value, field):
    require(
        isinstance(value, str)
        and bool(value)
        and not any(c in value for c in "\r\n\x00"),
        field,
    )
    return value


def port(value, field):
    require(type(value) is int and 1024 <= value <= 65535, field)
    return value


def url(value, field, https=False):
    text(value, field)
    try:
        p = urlsplit(value)
        valid = (
            p.scheme in (("https",) if https else ("http", "https"))
            and p.hostname
            and p.port != 0
        )
    except ValueError:
        valid = False
        p = None
    require(
        valid
        and p
        and not p.username
        and not p.password
        and not p.query
        and not p.fragment,
        field,
    )
    require(
        not any(c in value for c in '"\\<>') and not any(c.isspace() for c in value),
        field,
    )
    return value.rstrip("/")


def file_ref(value, field, must_exist=True):
    text(value, field)
    p = Path(value).expanduser()
    require(p.is_absolute() and (not must_exist or p.is_file()), field)
    return str(p)


def secret_env(name, field, resolve):
    require(isinstance(name, str) and ENV.fullmatch(name), field)
    if not resolve:
        return ""
    value = os.environ.get(name, "")
    # Deliberately restrict bootstrap/DB secrets to printable tokens: no systemd,
    # Keycloak property, SQL, or environment-file metacharacter interpretation.
    require(
        bool(re.fullmatch(r"[A-Za-z0-9_@%+=.,:/!-]{24,256}", value)),
        field + ".environment_value",
    )
    return value


def load_config(path, operation, *, resolve_secrets=True):
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError):
        raise ConfigError("cannot read configuration") from None
    return validate_config(raw, operation, resolve_secrets=resolve_secrets)


def validate_config(raw, operation, *, resolve_secrets=True):
    require(operation in OPERATIONS, "operation")
    exact_keys(
        raw,
        {
            "schema_version",
            "request_id",
            "cluster",
            "nodes",
            "endpoint",
            "install",
            "timeout_seconds",
        },
        "config",
    )
    c = copy.deepcopy(raw)
    require(
        type(c.get("schema_version")) is int and c["schema_version"] == 1,
        "schema_version",
    )
    ignored = (
        ("cluster", "nodes", "install")
        if operation == "inspect"
        else (("endpoint", "install") if operation == "discover" else ("endpoint",))
    )
    for section in ignored:
        c.pop(section, None)
    require(
        isinstance(c.get("request_id"), str) and ID.fullmatch(c["request_id"]),
        "request_id",
    )
    timeout = c.setdefault("timeout_seconds", 1800)
    require(type(timeout) is int and 10 <= timeout <= 7200, "timeout_seconds")
    if operation != "inspect":
        cluster = c.get("cluster")
        exact_keys(cluster, {"id", "kubeconfig", "context", "namespaces"}, "cluster")
        text(cluster.get("id"), "cluster.id")
        cluster["kubeconfig"] = file_ref(
            cluster.get("kubeconfig"), "cluster.kubeconfig"
        )
        text(cluster.get("context"), "cluster.context")
        namespaces = cluster.setdefault("namespaces", [])
        require(
            isinstance(namespaces, list)
            and all(isinstance(n, str) and ID.fullmatch(n) for n in namespaces),
            "cluster.namespaces",
        )
    nodes = c.setdefault("nodes", [])
    require(isinstance(nodes, list), "nodes")
    names = set()
    for n in nodes:
        exact_keys(
            n,
            {
                "name",
                "uid",
                "ssh_host",
                "ssh_port",
                "ssh_user",
                "private_key_file",
                "known_hosts_file",
                "become_password_env",
            },
            "nodes[]",
        )
        require(
            isinstance(n.get("name"), str)
            and ID.fullmatch(n["name"])
            and n["name"] not in names,
            "nodes[].name",
        )
        names.add(n["name"])
        text(n.get("uid"), "nodes[].uid")
        host = text(n.get("ssh_host"), "nodes[].ssh_host")
        require(
            re.fullmatch(r"[A-Za-z0-9_.:-]+", host) and not host.startswith("-"),
            "nodes[].ssh_host",
        )
        n.setdefault("ssh_port", 22)
        require(
            type(n["ssh_port"]) is int and 1 <= n["ssh_port"] <= 65535,
            "nodes[].ssh_port",
        )
        require(
            isinstance(n.get("ssh_user"), str)
            and re.fullmatch(r"[a-z_][a-z0-9_-]*", n["ssh_user"]),
            "nodes[].ssh_user",
        )
        for key in ("private_key_file", "known_hosts_file"):
            n[key] = file_ref(n.get(key), "nodes[]." + key)
        if "become_password_env" in n:
            require(
                isinstance(n["become_password_env"], str)
                and ENV.fullmatch(n["become_password_env"]),
                "nodes[].become_password_env",
            )
    if operation == "inspect":
        endpoint = c.get("endpoint")
        exact_keys(
            endpoint, {"base_url", "realm", "ca_file", "management_url"}, "endpoint"
        )
        endpoint["base_url"] = url(endpoint.get("base_url"), "endpoint.base_url")
        realm = endpoint.setdefault("realm", "master")
        require(
            isinstance(realm, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", realm),
            "endpoint.realm",
        )
        if "ca_file" in endpoint:
            endpoint["ca_file"] = file_ref(endpoint["ca_file"], "endpoint.ca_file")
        if "management_url" in endpoint:
            endpoint["management_url"] = url(
                endpoint["management_url"], "endpoint.management_url"
            )
    if operation in ("preflight", "install", "verify"):
        i = c.get("install")
        exact_keys(
            i,
            {
                "id",
                "node_name",
                "base_url",
                "https_port",
                "db_port",
                "certificate_file",
                "private_key_file",
                "ca_file",
                "db_password_env",
                "bootstrap_password_env",
                "min_memory_mb",
                "min_free_disk_mb",
            },
            "install",
        )
        require(isinstance(i.get("id"), str) and ID.fullmatch(i["id"]), "install.id")
        require(
            isinstance(i.get("node_name"), str) and i["node_name"] in names,
            "install.node_name",
        )
        i["base_url"] = url(i.get("base_url"), "install.base_url", https=True)
        p = urlsplit(i["base_url"])
        require(not p.path, "install.base_url.path")
        i.setdefault("https_port", 8443)
        i.setdefault("db_port", 55432)
        for key in ("https_port", "db_port"):
            port(i[key], "install." + key)
        require(
            i["https_port"] != i["db_port"]
            and not {7800, 9000, 57800}.intersection((i["https_port"], i["db_port"])),
            "install.port_collision",
        )
        require((p.port or 443) == i["https_port"], "install.base_url.port")
        for key in ("certificate_file", "private_key_file", "ca_file"):
            i[key] = file_ref(i.get(key), "install." + key)
        for key, default in (("min_memory_mb", 4096), ("min_free_disk_mb", 10240)):
            i.setdefault(key, default)
            require(type(i[key]) is int and i[key] >= 1024, "install." + key)
        # Public input never stores secret values. Only install resolves these.
        for key in ("db_password_env", "bootstrap_password_env"):
            value = secret_env(
                i.get(key), "install." + key, resolve_secrets and operation == "install"
            )
            if operation == "install":
                c.setdefault("secrets", {})[key.removesuffix("_env")] = value
    return c
