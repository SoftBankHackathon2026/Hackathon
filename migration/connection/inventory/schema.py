"""Strict inventory report boundary. No raw Kubernetes objects cross it."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

MAX_SAFE_INTEGER = 9_007_199_254_740_991
HEX384 = re.compile(r"sha384:[0-9a-f]{96}\Z")
TARGET_ID = re.compile(r"k8s-[0-9a-f]{48}\Z")


class ReportError(ValueError):
    """An intentionally payload-free validation error."""


def text(value: Any, *, empty: bool = False, limit: int = 256) -> str:
    if not isinstance(value, str) or len(value) > limit or (not value and not empty):
        raise ReportError("INVALID_TEXT")
    if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or ord(c) == 127 for c in value):
        raise ReportError("INVALID_TEXT")
    return value


def integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= MAX_SAFE_INTEGER:
        raise ReportError("INVALID_INTEGER")
    return value


def exact(value: Any, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        raise ReportError("INVALID_FIELDS")
    return value


def server_url(value: Any) -> str:
    value = text(value, limit=2048)
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError()
        if (
            parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError()
    except ValueError:
        raise ReportError("INVALID_SERVER_URL") from None
    return value.rstrip("/")


def canonical_json(value: Any) -> bytes:
    """JCS-compatible subset: ASCII field names, safe integers, no floats.

    All report keys are fixed ASCII strings. Reject unsupported values rather
    than silently relying on Python's differing float or Unicode-key ordering.
    """

    def check(item):
        if item is None or type(item) is bool:
            return
        if type(item) is int:
            if not -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
                raise ReportError("INVALID_INTEGER")
            return
        if isinstance(item, str):
            if any(0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise ReportError("INVALID_TEXT")
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if type(item) is dict:
            for key, child in item.items():
                if not isinstance(key, str) or not key.isascii():
                    raise ReportError("INVALID_FIELDS")
                check(child)
            return
        raise ReportError("INVALID_JSON_TYPE")

    check(value)
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def inventory_digest(report: dict) -> str:
    validate_report(report)
    return "sha384:" + hashlib.sha384(canonical_json(report)).hexdigest()


def target_id(context: str, server: str, ca_sha384: str, tls_server_name: str) -> str:
    descriptor = {
        "context": context,
        "server": server,
        "ca_sha384": ca_sha384,
        "tls_server_name": tls_server_name,
    }
    return "k8s-" + hashlib.sha384(canonical_json(descriptor)).hexdigest()[:48]


def validate_report(
    report: Any,
    *,
    expected_request_id: str | None = None,
    expected_device_id: str | None = None,
    expected_epoch: int | None = None,
    expected_target_id: str | None = None,
) -> dict:
    optional = (
        {"workloads"} if isinstance(report, dict) and "workloads" in report else set()
    )
    exact(
        report,
        {
            "schema_version",
            "request_id",
            "device_id",
            "epoch",
            "target_id",
            "collected_at_ms",
            "environment",
            "cluster",
            "resources",
            "coverage",
        }
        | optional,
    )
    if type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise ReportError("UNSUPPORTED_REPORT_VERSION")
    for field, expected in (
        ("request_id", expected_request_id),
        ("device_id", expected_device_id),
        ("target_id", expected_target_id),
    ):
        text(report[field])
        if expected is not None and report[field] != expected:
            raise ReportError("REPORT_BINDING_MISMATCH")
    if not TARGET_ID.fullmatch(report["target_id"]):
        raise ReportError("INVALID_TARGET_ID")
    integer(report["epoch"], minimum=1)
    if expected_epoch is not None and report["epoch"] != expected_epoch:
        raise ReportError("REPORT_BINDING_MISMATCH")
    integer(report["collected_at_ms"], minimum=1)
    env = exact(
        report["environment"],
        {"id", "kind", "name", "identity", "region", "context", "facts"},
    )
    if env["id"] != report["target_id"] or env["kind"] not in (
        "kubernetes",
        "aws",
        "gcp",
    ):
        raise ReportError("INVALID_ENVIRONMENT")
    for field in ("name", "identity", "context"):
        text(env[field])
    if env["kind"] == "kubernetes":
        if env["name"] != env["context"] or env["region"] != "":
            raise ReportError("INVALID_ENVIRONMENT")
    else:
        parts = env["context"].split("/")
        if (
            len(parts) != 3
            or not all(parts)
            or parts[1:] != [env["region"], env["name"]]
            or env["identity"] != env["context"]
        ):
            raise ReportError("INVALID_ENVIRONMENT")
    cluster = exact(
        report["cluster"],
        {"server", "ca_sha384", "tls_server_name", "namespace_uid", "version"},
    )
    server_url(cluster["server"])
    text(cluster["tls_server_name"], empty=True)
    if cluster["ca_sha384"] != "system-roots" and (
        not isinstance(cluster["ca_sha384"], str)
        or not HEX384.fullmatch(cluster["ca_sha384"])
    ):
        raise ReportError("INVALID_TRUST")
    for field in ("namespace_uid", "version"):
        text(cluster[field])
    if env["id"] != target_id(
        env["context"],
        cluster["server"],
        cluster["ca_sha384"],
        cluster["tls_server_name"],
    ):
        raise ReportError("REPORT_TARGET_CHANGED")
    if (
        env["kind"] == "kubernetes"
        and env["identity"] != "kubernetes:" + cluster["namespace_uid"]
    ):
        raise ReportError("INVALID_IDENTITY")
    resources = exact(
        report["resources"], {"node_count", "namespace_count", "storage_classes"}
    )
    coverage = exact(report["coverage"], {"nodes", "namespaces", "storage_classes"})
    for field in coverage:
        if coverage[field] not in ("complete", "unavailable"):
            raise ReportError("INVALID_COVERAGE")
    for category, field in (("nodes", "node_count"), ("namespaces", "namespace_count")):
        if coverage[category] == "complete":
            integer(resources[field])
        elif resources[field] is not None:
            raise ReportError("INVALID_COVERAGE")
    classes = resources["storage_classes"]
    if coverage["storage_classes"] == "unavailable":
        if classes is not None:
            raise ReportError("INVALID_COVERAGE")
    else:
        if not isinstance(classes, list) or len(classes) > 1024:
            raise ReportError("INVALID_STORAGE_CLASSES")
        names = set()
        for item in classes:
            exact(item, {"name", "provisioner", "default"})
            text(item["name"])
            text(item["provisioner"])
            if type(item["default"]) is not bool or item["name"] in names:
                raise ReportError("INVALID_STORAGE_CLASSES")
            names.add(item["name"])
    if env["facts"] != facts_for(cluster, resources):
        raise ReportError("INVALID_FACTS")
    if "workloads" in report:
        validate_workloads(report["workloads"])
    canonical_json(report)
    return report


def facts_for(cluster: dict, resources: dict) -> list[dict[str, str]]:
    return [
        {"label": "Kubernetes 버전", "value": cluster["version"]},
        {
            "label": "노드 수",
            "value": "미확인"
            if resources["node_count"] is None
            else str(resources["node_count"]),
        },
        {
            "label": "네임스페이스 수",
            "value": "미확인"
            if resources["namespace_count"] is None
            else str(resources["namespace_count"]),
        },
        {
            "label": "스토리지 클래스 수",
            "value": "미확인"
            if resources["storage_classes"] is None
            else str(len(resources["storage_classes"])),
        },
    ]


def validate_workloads(value):
    extra = (
        {"rolloutsAvailable"}
        if isinstance(value, dict) and "rolloutsAvailable" in value
        else set()
    )
    exact(value, {"coverage", "items"} | extra)
    if extra and type(value["rolloutsAvailable"]) is not bool:
        raise ReportError("INVALID_COVERAGE")
    if value["coverage"] not in ("complete", "unavailable"):
        raise ReportError("INVALID_COVERAGE")
    items = value["items"]
    if not isinstance(items, list) or len(items) > 2000:
        raise ReportError("INVALID_WORKLOADS")
    if value["coverage"] == "unavailable" and items:
        raise ReportError("INVALID_COVERAGE")
    ids = set()
    for item in items:
        exact(
            item,
            {
                "namespace",
                "kind",
                "name",
                "replicas",
                "images",
                "secrets",
                "configMaps",
                "pvcs",
            },
        )
        for key in ("namespace", "kind", "name"):
            text(item[key])
        if item["kind"] not in ("Deployment", "StatefulSet", "DaemonSet", "Rollout"):
            raise ReportError("INVALID_WORKLOAD_KIND")
        integer(item["replicas"])
        identity = (item["namespace"], item["kind"], item["name"])
        if identity in ids:
            raise ReportError("DUPLICATE_WORKLOAD")
        ids.add(identity)
        for key in ("images", "secrets", "configMaps", "pvcs"):
            if not isinstance(item[key], list) or len(item[key]) > 256:
                raise ReportError("INVALID_WORKLOAD_REFS")
            for name in item[key]:
                text(name, limit=1024)
    return value
