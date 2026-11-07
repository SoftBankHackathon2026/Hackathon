"""Local kubectl discovery and read-only, selected-context collection.

The caller must authenticate/authorize the task before invoking this module.
This module does not grant credentials, install resources or approve devices.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .schema import (
    ReportError,
    facts_for,
    integer,
    server_url,
    target_id,
    text,
    validate_report,
)

MAX_OUTPUT = 8 * 1024 * 1024
MAX_CONTEXTS = 100


class InventoryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def decode_json(raw: bytes):
    if len(raw) > MAX_OUTPUT:
        raise InventoryError("KUBECTL_RESPONSE_TOO_LARGE")
    try:
        return json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, RecursionError):
        raise InventoryError("INVALID_KUBECTL_RESPONSE") from None


def run_bounded(
    command: list[str],
    timeout: float = 20.0,
    *,
    classify_failure: Callable[[bytes], str] | None = None,
) -> bytes:
    """Keep responses only in bounded memory; never expose stderr/argv errors."""
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if classify_failure else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        raise InventoryError("KUBECTL_START_FAILED") from None
    data = bytearray()
    errors = bytearray()
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if process.stderr is not None:
                selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InventoryError("KUBECTL_TIMEOUT")
                for key, _ in selector.select(min(remaining, 0.2)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is process.stderr:
                        errors.extend(block[: max(0, 16384 - len(errors))])
                        continue
                    data.extend(block)
                    if len(data) > MAX_OUTPUT:
                        raise InventoryError("KUBECTL_RESPONSE_TOO_LARGE")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InventoryError("KUBECTL_TIMEOUT")
            try:
                status = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise InventoryError("KUBECTL_TIMEOUT") from None
            if status != 0:
                raise InventoryError(
                    classify_failure(bytes(errors))
                    if classify_failure
                    else "KUBECTL_REQUEST_FAILED"
                )
        return bytes(data)
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


class Kubectl:
    def __init__(
        self,
        *,
        kubeconfig: str | None = None,
        runner: Callable[[list[str]], bytes] = run_bounded,
        executable: str | None = None,
    ):
        # Configuration/executable are trusted local agent configuration, never
        # accepted from a remote task payload.
        binary = executable or shutil.which("kubectl")
        if not binary:
            raise InventoryError("KUBECTL_NOT_INSTALLED")
        self.base = [str(Path(binary).resolve())]
        if kubeconfig:
            self.base.append("--kubeconfig=" + str(Path(kubeconfig).resolve()))
        self.runner = runner

    def json(self, args: list[str], *, context: str | None = None):
        command = self.base + (["--context=" + context] if context is not None else [])
        command += ["--request-timeout=15s"] + args
        return decode_json(self.runner(command))

    def descriptors(self) -> list[dict]:
        # config view is local; unlike a get command it does not invoke cluster
        # authentication or contact any Kubernetes API server.
        contexts = self.json(["config", "view", "--output=jsonpath={.contexts}"])
        if not isinstance(contexts, list) or len(contexts) > MAX_CONTEXTS:
            raise InventoryError("INVALID_CONTEXT_LIST")
        result, names = [], set()
        for item in contexts:
            try:
                name = text(item["name"])
                if name in names:
                    raise ReportError("DUPLICATE_CONTEXT")
                names.add(name)
                # Only the PUBLIC cluster trust object is projected. User auth
                # entries (tokens, exec args, private keys) are never requested.
                cluster = self.json(
                    [
                        "config",
                        "view",
                        "--minify",
                        "--flatten",
                        "--raw",
                        "--output=jsonpath={.clusters[0].cluster}",
                    ],
                    context=name,
                )
                if type(cluster) is not dict:
                    raise ReportError("INVALID_CLUSTER")
                if cluster.get("insecure-skip-tls-verify") or cluster.get("proxy-url"):
                    raise ReportError("UNSUPPORTED_CLUSTER_TRUST")
                server = server_url(cluster.get("server"))
                tls_name = text(cluster.get("tls-server-name", ""), empty=True)
                encoded = cluster.get("certificate-authority-data")
                trust = "system-roots"
                if encoded:
                    if not isinstance(encoded, str) or len(encoded) > 131072:
                        raise ReportError("INVALID_CA")
                    cert = base64.b64decode(encoded, validate=True)
                    if not cert or b"-----BEGIN CERTIFICATE-----" not in cert:
                        raise ReportError("INVALID_CA")
                    trust = "sha384:" + hashlib.sha384(cert).hexdigest()
                elif cluster.get("certificate-authority"):
                    raise ReportError("UNFLATTENED_CA")
                result.append(
                    {
                        "id": target_id(name, server, trust, tls_name),
                        "context": name,
                        "server": server,
                        "ca_sha384": trust,
                        "tls_server_name": tls_name,
                    }
                )
            except (ReportError, KeyError, TypeError, ValueError):
                # Do not silently call an insecure/changed context or leak the
                # source config through an exception. User can repair locally.
                raise InventoryError("UNSUPPORTED_CONTEXT_CONFIGURATION") from None
        return sorted(result, key=lambda d: d["context"])


def _binding(request_id: str, device_id: str, epoch: int):
    try:
        text(request_id)
        text(device_id)
        integer(epoch, minimum=1)
    except ReportError:
        raise InventoryError("INVALID_TASK_BINDING") from None


def discover(kubectl: Kubectl, *, request_id: str, device_id: str, epoch: int) -> dict:
    _binding(request_id, device_id, epoch)
    descriptors = kubectl.descriptors()
    return {
        "schema_version": 1,
        "request_id": request_id,
        "device_id": device_id,
        "epoch": epoch,
        "discovered": [
            {
                "id": d["id"],
                "kind": "kubernetes",
                "name": d["context"],
                "identity": d["server"],
                "region": "",
                "context": d["context"],
            }
            for d in descriptors
        ],
    }


def _items(value):
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise InventoryError("INVALID_KUBECTL_RESPONSE")
    return value["items"]


def collect(
    kubectl: Kubectl,
    *,
    request_id: str,
    device_id: str,
    epoch: int,
    selected_target_id: str,
    clock: Callable[[], float] = time.time,
    cloud_environment: dict | None = None,
) -> dict:
    _binding(request_id, device_id, epoch)
    descriptors = kubectl.descriptors()
    selected = next((d for d in descriptors if d["id"] == selected_target_id), None)
    if selected is None:
        raise InventoryError("SELECTED_TARGET_CHANGED_OR_MISSING")
    context = selected["context"]
    # A successful request proves access under kubectl's existing TLS/auth
    # configuration. kube-system UID distinguishes similarly named clusters.
    namespace = kubectl.json(
        ["get", "namespace", "kube-system", "--output=json"], context=context
    )
    version = kubectl.json(["version", "--output=json"], context=context)
    try:
        uid = text(namespace["metadata"]["uid"])
        git_version = text(version["serverVersion"]["gitVersion"])
    except (KeyError, TypeError, ReportError):
        raise InventoryError("CLUSTER_IDENTITY_UNVERIFIED") from None
    cluster = {
        "server": selected["server"],
        "ca_sha384": selected["ca_sha384"],
        "tls_server_name": selected["tls_server_name"],
        "namespace_uid": uid,
        "version": git_version,
    }
    resources = {"node_count": None, "namespace_count": None, "storage_classes": None}
    coverage = {
        "nodes": "unavailable",
        "namespaces": "unavailable",
        "storage_classes": "unavailable",
    }
    for resource, field in (("nodes", "node_count"), ("namespaces", "namespace_count")):
        try:
            resources[field] = len(
                _items(
                    kubectl.json(["get", resource, "--output=json"], context=context)
                )
            )
            coverage[resource] = "complete"
        except InventoryError:
            pass
    try:
        items = _items(
            kubectl.json(
                ["get", "storageclasses.storage.k8s.io", "--output=json"],
                context=context,
            )
        )
        if len(items) > 1024:
            raise InventoryError("TOO_MANY_STORAGE_CLASSES")
        safe_classes = []
        for item in items:
            metadata = item["metadata"]
            annotations = metadata.get("annotations", {})
            safe_classes.append(
                {
                    "name": text(metadata["name"]),
                    "provisioner": text(item["provisioner"]),
                    "default": annotations.get(
                        "storageclass.kubernetes.io/is-default-class"
                    )
                    == "true"
                    or annotations.get(
                        "storageclass.beta.kubernetes.io/is-default-class"
                    )
                    == "true",
                }
            )
        resources["storage_classes"] = sorted(safe_classes, key=lambda v: v["name"])
        coverage["storage_classes"] = "complete"
    except (InventoryError, KeyError, TypeError, ReportError):
        pass
    workloads = {"coverage": "unavailable", "items": []}
    try:
        raw = _items(
            kubectl.json(
                [
                    "get",
                    "deployments.apps,statefulsets.apps,daemonsets.apps",
                    "--all-namespaces",
                    "--output=json",
                ],
                context=context,
            )
        )
        # Argo Rollouts is optional; absence/denial must not hide standard apps.
        rollouts_available = False
        try:
            raw += _items(
                kubectl.json(
                    [
                        "get",
                        "rollouts.argoproj.io",
                        "--all-namespaces",
                        "--output=json",
                    ],
                    context=context,
                )
            )
            rollouts_available = True
        except InventoryError:
            pass
        summaries = []
        for item in raw:
            spec = item.get("spec", {})
            pod = spec.get("template", {}).get("spec", {})
            containers = pod.get("containers", []) + pod.get("initContainers", [])
            secrets, configs, pvcs = set(), set(), set()
            for c in containers:
                for env in c.get("env", []):
                    ref = env.get("valueFrom", {})
                    for key, dest in (
                        ("secretKeyRef", secrets),
                        ("configMapKeyRef", configs),
                    ):
                        if ref.get(key, {}).get("name"):
                            dest.add(ref[key]["name"])
                for env in c.get("envFrom", []):
                    for key, dest in (
                        ("secretRef", secrets),
                        ("configMapRef", configs),
                    ):
                        if env.get(key, {}).get("name"):
                            dest.add(env[key]["name"])
            for volume in pod.get("volumes", []):
                for key, field, dest in (
                    ("secret", "secretName", secrets),
                    ("configMap", "name", configs),
                    ("persistentVolumeClaim", "claimName", pvcs),
                ):
                    if volume.get(key, {}).get(field):
                        dest.add(volume[key][field])
            summaries.append(
                {
                    "namespace": item["metadata"]["namespace"],
                    "name": item["metadata"]["name"],
                    "kind": item["kind"],
                    "replicas": spec.get(
                        "replicas",
                        item.get("status", {}).get("desiredNumberScheduled", 0),
                    ),
                    "images": sorted({c["image"] for c in containers}),
                    "secrets": sorted(secrets),
                    "configMaps": sorted(configs),
                    "pvcs": sorted(pvcs),
                }
            )
        from .schema import validate_workloads

        workloads = validate_workloads(
            {
                "coverage": "complete",
                "items": summaries,
                "rolloutsAvailable": rollouts_available,
            }
        )
    except (InventoryError, ReportError, KeyError, TypeError):
        workloads = {"coverage": "unavailable", "items": []}
    # Recheck local context/trust binding after calls. A kubeconfig edit during
    # collection invalidates all results rather than assigning them to old UI.
    if selected not in kubectl.descriptors():
        raise InventoryError("SELECTED_TARGET_CHANGED_OR_MISSING")
    environment = {
        "id": selected["id"],
        "kind": "kubernetes",
        "name": context,
        "identity": "kubernetes:" + uid,
        "region": "",
        "context": context,
        "facts": facts_for(cluster, resources),
    }
    if cloud_environment:
        environment.update(cloud_environment)
    report = {
        "schema_version": 1,
        "request_id": request_id,
        "device_id": device_id,
        "epoch": epoch,
        "target_id": selected["id"],
        "collected_at_ms": int(clock() * 1000),
        "environment": environment,
        "cluster": cluster,
        "resources": resources,
        "coverage": coverage,
        "workloads": workloads,
    }
    try:
        return validate_report(report)
    except ReportError:
        raise InventoryError("INVALID_INVENTORY_RESULT") from None
