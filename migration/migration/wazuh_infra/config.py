"""Strict platform input. Error messages contain field names, never supplied values."""

from __future__ import annotations

import configparser
import copy
import hashlib
import ipaddress
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).parent / "ansible"
OPERATIONS = (
    "discover",
    "inspect",
    "assess",
    "preflight",
    "install",
    "configure",
    "verify",
    "control",
)
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
S3_TYPES = {
    "cloudtrail",
    "vpcflow",
    "config",
    "guardduty",
    "waf",
    "server_access",
    "custom",
}


class ConfigError(ValueError):
    pass


def require(condition, field):
    if not condition:
        raise ConfigError("invalid or missing field: " + field)


def keys(obj, allowed, field):
    require(isinstance(obj, dict) and not set(obj) - set(allowed.split()), field)


def text(value, field, pattern=ID):
    require(isinstance(value, str) and pattern.fullmatch(value), field)
    return value


def integer(value, field, low=0, high=2**63 - 1):
    require(type(value) is int and low <= value <= high, field)
    return value


def boolean(value, field):
    require(type(value) is bool, field)
    return value


def absolute(value, field):
    require(isinstance(value, str) and value.startswith("/"), field)
    require(not any(x in value for x in ("\x00", "\n", "\r", "..", "{{", "{%")), field)
    require(re.fullmatch(r"/[A-Za-z0-9_./-]+", value) is not None, field)
    require(str(Path(value)) == value and value != "/", field)
    return value


def local_file(value, field, private=False):
    absolute(value, field)
    try:
        fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode), field)
            if private:
                require(
                    info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
                    field + ".permissions",
                )
            require(info.st_size <= 1024 * 1024, field + ".size")
    except OSError:
        raise ConfigError("unreadable file reference: " + field) from None
    return value


def secret(value):
    local_file(value, "secret_ref", private=True)
    fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return stream.read(1024 * 1024)


def endpoint(value, field):
    try:
        u = urlsplit(value)
        require(
            u.scheme == "https" and u.hostname and not u.username and not u.password,
            field,
        )
        require(not u.query and not u.fragment and u.path in ("", "/"), field)
        require(not any(c.isspace() or c in '\\"{}' for c in value), field)
        integer(u.port or 443, field, 1, 65535)
    except (ValueError, TypeError):
        raise ConfigError("invalid or missing field: " + field) from None
    return value.rstrip("/")


def validate(raw, operation):
    require(operation in OPERATIONS, "operation")
    keys(
        raw,
        "schema_version request_id timeout_seconds cluster nodes target endpoint storage sources tls control verification access",
        "config",
    )
    c = copy.deepcopy(raw)
    require(
        type(c.get("schema_version")) is int and c["schema_version"] == 1,
        "schema_version",
    )
    text(c.get("request_id"), "request_id")
    c["timeout_seconds"] = integer(
        c.get("timeout_seconds", 1800), "timeout_seconds", 10, 7200
    )
    cluster = c.setdefault("cluster", {})
    keys(cluster, "id kubeconfig context namespaces", "cluster")
    if cluster:
        text(cluster.get("id"), "cluster.id")
        local_file(cluster.get("kubeconfig"), "cluster.kubeconfig", private=True)
        require(
            isinstance(cluster.get("context"), str) and bool(cluster["context"]),
            "cluster.context",
        )
        require(isinstance(cluster.get("namespaces", []), list), "cluster.namespaces")
        for ns in cluster.get("namespaces", []):
            text(ns, "cluster.namespaces", HOST)
    nodes = c.setdefault("nodes", [])
    require(isinstance(nodes, list), "nodes")
    seen = set()
    for n in nodes:
        keys(
            n,
            "name uid ssh_host ssh_port ssh_user private_key_file known_hosts_file become_password_ref",
            "nodes",
        )
        text(n.get("name"), "nodes.name", HOST)
        text(n.get("uid"), "nodes.uid")
        require(n["name"] not in seen, "nodes.duplicate")
        seen.add(n["name"])
        text(n.get("ssh_host"), "nodes.ssh_host", HOST)
        text(n.get("ssh_user"), "nodes.ssh_user")
        n["ssh_port"] = integer(n.get("ssh_port", 22), "nodes.ssh_port", 1, 65535)
        local_file(n.get("private_key_file"), "nodes.private_key_file", private=True)
        local_file(n.get("known_hosts_file"), "nodes.known_hosts_file")
        if n.get("become_password_ref"):
            local_file(
                n["become_password_ref"], "nodes.become_password_ref", private=True
            )
    t = c.setdefault("target", {})
    keys(
        t,
        "installation_id node_name bind_address dashboard_port min_available_memory_mb heap_mb allow_control_plane",
        "target",
    )
    if operation not in ("inspect", "discover"):
        require(cluster and nodes, "cluster.nodes")
        text(t.get("installation_id"), "target.installation_id")
        require(t.get("node_name") in seen, "target.node_name")
        try:
            addr = ipaddress.IPv4Address(t.get("bind_address"))
            require(
                not addr.is_unspecified
                and not addr.is_multicast
                and not addr.is_loopback,
                "target.bind_address",
            )
        except (ipaddress.AddressValueError, TypeError):
            raise ConfigError("invalid or missing field: target.bind_address") from None
    t["dashboard_port"] = integer(
        t.get("dashboard_port", 8443), "target.dashboard_port", 1024, 65535
    )
    t["heap_mb"] = integer(t.get("heap_mb", 2048), "target.heap_mb", 1024, 32768)
    t["min_available_memory_mb"] = integer(
        t.get("min_available_memory_mb", 8192), "target.min_available_memory_mb", 4096
    )
    t["allow_control_plane"] = boolean(
        t.get("allow_control_plane", False), "target.allow_control_plane"
    )
    ep = c.setdefault("endpoint", {})
    keys(ep, "url ca_file username password_ref", "endpoint")
    if ep:
        ep["url"] = endpoint(ep.get("url"), "endpoint.url")
        local_file(ep.get("ca_file"), "endpoint.ca_file")
        if ep.get("password_ref"):
            text(ep.get("username"), "endpoint.username")
            local_file(ep["password_ref"], "endpoint.password_ref", private=True)
    require(operation != "inspect" or bool(ep), "endpoint")
    st = c.setdefault("storage", {})
    keys(
        st,
        "raw_path index_path reserve_bytes raw_budget_bytes index_budget_bytes intermediate_budget_bytes source_raw_bytes decoded_bytes index_factor_low index_factor_high daily_raw_bytes",
        "storage",
    )
    for k, default in (
        ("raw_path", "/srv/oneport-audit/raw"),
        ("index_path", "/srv/oneport-audit/index"),
    ):
        st[k] = absolute(st.get(k, default), "storage." + k)
        require(st[k].startswith(("/srv/", "/mnt/", "/data/")), "storage." + k)
    require(
        not any(
            a == b or a.startswith(b + "/")
            for a, b in (
                (st["raw_path"], st["index_path"]),
                (st["index_path"], st["raw_path"]),
            )
        ),
        "storage.overlap",
    )
    for k, default in (
        ("reserve_bytes", 10 * 1024**3),
        ("raw_budget_bytes", 10 * 1024**3),
        ("index_budget_bytes", 20 * 1024**3),
        ("intermediate_budget_bytes", 5 * 1024**3),
    ):
        st[k] = integer(st.get(k, default), "storage." + k, 1)
    for k in ("source_raw_bytes", "decoded_bytes", "daily_raw_bytes"):
        if st.get(k) is not None:
            integer(st[k], "storage." + k)
    for k in ("index_factor_low", "index_factor_high"):
        if st.get(k) is not None:
            require(type(st[k]) in (int, float) and 0 < st[k] <= 1000, "storage." + k)
    if st.get("index_factor_low") and st.get("index_factor_high"):
        require(
            st["index_factor_low"] <= st["index_factor_high"],
            "storage.index_factor_range",
        )
    sources = c.setdefault("sources", [])
    require(isinstance(sources, list), "sources")
    control_input = c.get("control", {})
    need_source_credentials = operation in ("install", "configure", "assess") or (
        operation == "control"
        and isinstance(control_input, dict)
        and control_input.get("action") == "start"
    )
    ids = set()
    for s in sources:
        keys(
            s,
            "id type bucket bucket_owner prefix log_group account_id region credentials_file profile start_date max_pages",
            "sources",
        )
        text(s.get("id"), "sources.id")
        require(s["id"] not in ids, "sources.duplicate")
        ids.add(s["id"])
        require(s.get("type") in S3_TYPES | {"cloudwatchlogs"}, "sources.type")
        if s["type"] == "cloudwatchlogs":
            text(
                s.get("log_group"),
                "sources.log_group",
                re.compile(r"[A-Za-z0-9_./#-]{1,512}\Z"),
            )
            require(
                not any(k in s for k in ("bucket", "bucket_owner", "prefix")),
                "sources.cloudwatch_fields",
            )
        else:
            text(
                s.get("bucket"),
                "sources.bucket",
                re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z"),
            )
            require("log_group" not in s, "sources.s3_fields")
            s["bucket_owner"] = text(
                s.get("bucket_owner", s.get("account_id")),
                "sources.bucket_owner",
                re.compile(r"\d{12}\Z"),
            )
        text(s.get("account_id"), "sources.account_id", re.compile(r"\d{12}\Z"))
        text(
            s.get("region"), "sources.region", re.compile(r"[a-z]{2}(?:-[a-z]+)+-\d\Z")
        )
        prefix = s.get("prefix", "")
        require(
            isinstance(prefix, str)
            and re.fullmatch(r"[A-Za-z0-9_./=-]*", prefix) is not None
            and ".." not in prefix,
            "sources.prefix",
        )
        if s["type"] != "cloudwatchlogs":
            s["prefix"] = prefix
        s["profile"] = text(s.get("profile", "default"), "sources.profile")
        absolute(s.get("credentials_file"), "sources.credentials_file")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            if need_source_credentials:
                parser.read_string(secret(s["credentials_file"]))
                require(parser.has_section(s["profile"]), "sources.profile")
                require(
                    all(
                        parser.has_option(s["profile"], k)
                        for k in ("aws_access_key_id", "aws_secret_access_key")
                    ),
                    "sources.credentials_file",
                )
        except configparser.Error:
            raise ConfigError(
                "invalid or missing field: sources.credentials_file"
            ) from None
        s["start_date"] = s.get("start_date", "1970-JAN-01")
        try:
            datetime.strptime(s["start_date"], "%Y-%b-%d")
        except (ValueError, TypeError):
            raise ConfigError("invalid or missing field: sources.start_date") from None
        s["max_pages"] = integer(s.get("max_pages", 10), "sources.max_pages", 1, 1000)
    tls = c.setdefault("tls", {})
    keys(tls, "mode certificate_ref private_key_ref ca_ref", "tls")
    require(
        tls.setdefault("mode", "self_signed") in ("self_signed", "provided"), "tls.mode"
    )
    if tls["mode"] == "provided":
        for k in ("certificate_ref", "private_key_ref", "ca_ref"):
            if operation in ("install", "configure"):
                local_file(tls.get(k), "tls." + k, private=k == "private_key_ref")
            else:
                absolute(tls.get(k), "tls." + k)
    ctl = c.setdefault("control", {})
    keys(ctl, "action", "control")
    require(
        ctl.setdefault("action", "status") in ("start", "pause", "status"),
        "control.action",
    )
    v = c.setdefault("verification", {})
    keys(v, "probe", "verification")
    boolean(v.setdefault("probe", False), "verification.probe")
    access = c.setdefault("access", {})
    keys(access, "operator_password_ref reader_password_ref", "access")
    if operation in ("install", "configure"):
        for k in ("operator_password_ref", "reader_password_ref"):
            local_file(access.get(k), "access." + k, private=True)
            require(
                16 <= len(secret(access[k]).strip().encode()) <= 72,
                "access." + k + ".length",
            )
        require(
            secret(access["operator_password_ref"]).strip()
            != secret(access["reader_password_ref"]).strip(),
            "access.distinct_passwords",
        )
    return c


def configuration_digest(c):
    """Exclude invocation fields; do not include credential contents in the marker."""
    data = {k: c[k] for k in ("target", "storage", "sources", "tls")}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
