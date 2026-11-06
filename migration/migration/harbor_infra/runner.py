"""Private Ansible execution, target binding, plan hashes and safe aggregation."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import cluster, handoff
from .ansible.module_utils.harbor_core import (
    HarborError,
    atomic,
    check,
    digest,
    private_dir,
)
from .config import ASSETS, ConfigError, bootstrap_secret, require, secret_file


def plan_digest(config):
    c = copy.deepcopy(config)
    for key in ("request_id", "plan_digest", "timeout_seconds", "verification"):
        c.pop(key, None)
    if c.get("handoff"):
        for key in ("action", "receipt_digest"):
            c["handoff"].pop(key, None)

    # Bind protected input contents, but expose only one digest of the entire plan.
    def normalize(v):
        if isinstance(v, dict):
            return {
                k: {
                    "path": value,
                    "sha256": hashlib.sha256(Path(value).read_bytes()).hexdigest(),
                }
                if k.endswith("_ref") and isinstance(value, str)
                else normalize(value)
                for k, value in v.items()
            }
        if isinstance(v, list):
            return [normalize(x) for x in v]
        return v

    return digest(
        {
            "config": normalize(c),
            "releases": json.loads((ASSETS / "releases.json").read_text()),
        }
    )


def installation_digest(c):
    # Access resources and restart approvals may evolve without reinstalling data.
    values = {
        "organization_id": c["organization_id"],
        "target": c["target"],
        "endpoint": c["endpoint"]["url"],
        "install": c["install"],
        "tls": c["tls"],
        "admin_password_ref": c["endpoint"]["admin_password_ref"],
    }
    return plan_digest(values)


def resolve(c, operation):
    values = {}
    ep = c.get("endpoint", {})
    if ep.get("ca_ref"):
        values["endpoint_ca"] = Path(ep["ca_ref"]).read_text()
    elif c.get("handoff") and c.get("target"):
        ca_path = handoff.state_directory(c) / "trusted-ca.pem"
        if ca_path.is_file():
            require(not ca_path.is_symlink(), "generated_ca_ref")
            values["endpoint_ca"] = ca_path.read_text()
    if operation in (
        "inspect",
        "preflight",
        "configure",
        "verify",
        "install",
    ) and ep.get("admin_password_ref"):
        values["admin_password"] = secret_file(ep["admin_password_ref"])
    if (
        operation in ("install", "preflight")
        and c.get("target", {}).get("mode") == "new"
    ):
        if "admin_password" not in values:
            values["admin_password"] = secret_file(ep["admin_password_ref"])
        bootstrap_secret(values["admin_password"], "admin_password")
        values["database_password"] = secret_file(c["install"]["database_password_ref"])
        bootstrap_secret(values["database_password"], "database_password")
        if c["tls"]["mode"] == "provided":
            values.update(
                tls_certificate=Path(c["tls"]["certificate_ref"]).read_text(),
                tls_ca=Path(c["tls"]["ca_ref"]).read_text(),
                tls_private_key=secret_file(c["tls"]["private_key_ref"]),
            )
            validate_certificate(c, values)
    if c.get("keycloak") and operation in ("configure", "install"):
        values["oidc_client_secret"] = secret_file(c["keycloak"]["client_secret_ref"])
        if c["keycloak"].get("ca_ref"):
            values["oidc_ca"] = Path(c["keycloak"]["ca_ref"]).read_text()
        if c["keycloak"].get("manage_client"):
            values["keycloak_admin_token"] = secret_file(
                c["keycloak"]["admin_token_ref"]
            )
    return values


def validate_certificate(c, values):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_pem_x509_certificate(values["tls_certificate"].encode())
    key = serialization.load_pem_private_key(
        values["tls_private_key"].encode(), password=None
    )
    require(
        cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        == key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
        "tls.key_match",
    )
    require(
        cert.not_valid_after_utc > datetime.now(timezone.utc)
        and cert.not_valid_before_utc <= datetime.now(timezone.utc),
        "tls.validity",
    )
    host = urlsplit(c["endpoint"]["url"]).hostname
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    import ipaddress

    try:
        require(
            ipaddress.ip_address(host) in sans.get_values_for_type(x509.IPAddress),
            "tls.address_match",
        )
    except ValueError:
        require(host in sans.get_values_for_type(x509.DNSName), "tls.address_match")


def render_harbor(c, values):
    model = yaml.safe_load((ASSETS / "templates/harbor.yml.tmpl").read_text())
    u = urlsplit(c["endpoint"]["url"])
    root = "/opt/oneport-harbor/" + c["target"]["installation_id"]
    model.pop("http", None)
    model.update(
        hostname=u.hostname,
        external_url=c["endpoint"]["url"],
        harbor_admin_password=values["admin_password"],
        data_volume=c["install"]["data_path"],
    )
    model["log"]["local"]["location"] = (
        "/var/log/oneport-harbor/" + c["target"]["installation_id"]
    )
    model["log"]["local"].update(rotate_count=5, rotate_size="20M")
    model["database"]["password"] = values["database_password"]
    model["https"] = {
        "port": u.port or 443,
        "certificate": root + "/certificates/server.crt",
        "private_key": root + "/certificates/server.key",
    }
    return yaml.safe_dump(model, sort_keys=False)


def ansible(operation, c, nodes, values, cluster_report):
    bound = Path(sys.executable).parent / "ansible-playbook"
    exe = str(bound) if bound.is_file() else shutil.which("ansible-playbook")
    if not exe:
        raise HarborError("ANSIBLE_NOT_INSTALLED")
    with tempfile.TemporaryDirectory(prefix="harbor-infra-") as name:
        work = Path(name)
        work.chmod(0o700)
        reports = {n["name"]: n for n in cluster_report.get("nodes", [])}
        hosts = {}
        for index, node in enumerate(nodes):
            item = {
                "ansible_host": node["ssh_host"],
                "ansible_port": node["ssh_port"],
                "ansible_user": node["ssh_user"],
                "ansible_ssh_private_key_file": node["private_key_ref"],
                "ansible_ssh_common_args": "-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="
                + shlex.quote(node["known_hosts_ref"]),
                "ansible_python_interpreter": "/usr/bin/python3",
                "harbor_node": reports[node["name"]],
            }
            if node.get("become_password_ref"):
                item["ansible_become_password"] = secret_file(
                    node["become_password_ref"]
                )
            hosts["node_" + str(index)] = item
        inventory = {
            "all": {
                "children": {
                    "harbor_nodes": {"hosts": hosts},
                    "controllers": {
                        "hosts": {
                            "controller": {
                                "ansible_connection": "local",
                                "ansible_python_interpreter": sys.executable,
                            }
                        }
                    },
                }
            }
        }
        state = (
            str(handoff.state_directory(c))
            if c.get("handoff") and c.get("target")
            else ""
        )
        variables = {
            "harbor_operation": operation,
            "harbor_config": c,
            "harbor_secrets": values,
            "harbor_releases": json.loads((ASSETS / "releases.json").read_text()),
            "harbor_yaml": render_harbor(c, values) if operation == "install" else "",
            "harbor_state_directory": state,
        }
        for file, value in [("inventory.json", inventory), ("vars.json", variables)]:
            atomic(work / file, json.dumps(value))
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("ANSIBLE_", "HARBOR_"))
        }
        env.update(
            ANSIBLE_CONFIG=str(ASSETS / "ansible.cfg"),
            ANSIBLE_LIBRARY=str(ASSETS / "library"),
            ANSIBLE_MODULE_UTILS=str(ASSETS / "module_utils"),
            ANSIBLE_CALLBACK_PLUGINS=str(ASSETS / "callback_plugins"),
            ANSIBLE_CALLBACKS_ENABLED="harbor_events",
            ANSIBLE_LOG_PATH=os.devnull,
            ANSIBLE_HOME=str(work / "home"),
            ANSIBLE_LOCAL_TEMP=str(work / "local"),
            ANSIBLE_NOCOLOR="1",
            HARBOR_EVENTS_FILE=str(work / "events.jsonl"),
        )
        with (work / "stdout").open("wb") as output:
            proc = subprocess.Popen(
                [
                    exe,
                    "-i",
                    str(work / "inventory.json"),
                    str(ASSETS / "playbooks" / (operation + ".yml")),
                    "-e",
                    "@" + str(work / "vars.json"),
                ],
                cwd=ASSETS,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=c["timeout_seconds"])
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise HarborError(
                    "EXECUTION_INTERRUPTED_REMOTE_STATE_UNKNOWN"
                ) from None
        event_path = work / "events.jsonl"
        events = (
            [json.loads(line) for line in event_path.read_text().splitlines()]
            if event_path.exists()
            else []
        )
        return events, proc.returncode


def run(operation, config):
    c = copy.deepcopy(config)
    started = datetime.now(timezone.utc).isoformat()
    base = {
        "schema_version": 1,
        "request_id": c["request_id"],
        "operation": operation,
        "scope": "infrastructure_only",
        "workload_image_transfer_performed": False,
        "started_at": started,
        "checks": [],
        "reports": [],
        "reason_codes": [],
        "changed": False,
    }
    try:
        computed = plan_digest(c)
        if operation in ("install", "configure", "trust"):
            check(c["plan_digest"] == computed, "PLAN_CHANGED")
        c["plan_digest"] = computed
        if c.get("target", {}).get("mode") == "new" and c.get("install"):
            c["installation_digest"] = installation_digest(c)
        base["plan_digest"] = computed
        if operation == "handoff":
            base.update(handoff.execute(c))
            return base
        cluster_report = {}
        needs_cluster = (
            operation in ("discover", "trust")
            or operation in ("preflight", "verify")
            and bool(c["trust"]["targets"])
            or (
                operation in ("preflight", "install", "verify")
                and c.get("target", {}).get("mode") == "new"
            )
        )
        if needs_cluster:
            try:
                cluster_report = cluster.collect(c, operation == "discover")
            except ConfigError:
                raise
            except Exception:
                raise HarborError("CLUSTER_READ_FAILED") from None
            base["reports"].append(cluster_report)
            base["reason_codes"].extend(cluster_report.get("reason_codes", []))
        values = resolve(c, operation)
        if operation == "trust":
            check(bool(values.get("endpoint_ca")), "TRUST_CA_REFERENCE_REQUIRED")
        nodes = c["nodes"] if operation == "discover" else []
        if (
            operation in ("install", "verify")
            and c["target"]["mode"] == "new"
            or operation == "preflight"
            and c["target"]["mode"] == "new"
        ):
            nodes = [n for n in c["nodes"] if n["name"] == c["target"]["node_name"]]
        if operation == "trust":
            nodes = [n for n in c["nodes"] if n["name"] in c["trust"]["targets"]]
        actual = operation
        if operation == "preflight" and c["target"]["mode"] == "reuse":
            actual = "inspect"
        if operation == "verify":
            actual = "verify" if c["target"]["mode"] == "new" else "inspect"
        events, rc = ansible(actual, c, nodes, values, cluster_report)
        for e in events:
            base["changed"] |= e.get("changed", False)
            if e.get("failed"):
                base["reason_codes"].append(e.get("reason_code", "ANSIBLE_TASK_FAILED"))
                base["changed_unknown"] = True
            if e.get("report"):
                base["reports"].append(e["report"])
                base["reason_codes"].extend(e["report"].get("reason_codes", []))
        reports = [e["report"] for e in events if e.get("report")]
        base["status"] = (
            "failed"
            if rc
            else reports[-1].get("status", "partially_ready")
            if reports
            else "partially_ready"
        )
        if rc:
            base["reason_codes"].append("ANSIBLE_EXECUTION_FAILED")
        if not rc and operation == "discover":
            base["status"] = "partially_ready" if base["reason_codes"] else "discovered"
        if not rc and operation == "preflight":
            base["status"] = (
                "preflight_passed"
                if base["status"] in ("inspected", "preflight_passed")
                else "partially_ready"
            )
            if c["trust"]["targets"] and not values.get("endpoint_ca"):
                base["checks"].append(
                    {
                        "name": "trust_preflight",
                        "status": "not_run",
                        "reason_code": "AWAITING_GENERATED_CA"
                        if c["target"]["mode"] == "new"
                        else "TRUST_CA_REFERENCE_REQUIRED",
                    }
                )
                if c["target"]["mode"] == "reuse":
                    base["status"] = "partially_ready"
            if c["trust"]["targets"] and values.get("endpoint_ca"):
                trust_nodes = [
                    n for n in c["nodes"] if n["name"] in c["trust"]["targets"]
                ]
                trust_events, trust_rc = ansible(
                    "trust_preflight", c, trust_nodes, values, cluster_report
                )
                base["reports"].extend(
                    e["report"] for e in trust_events if e.get("report")
                )
                for event in trust_events:
                    report = event.get("report", {})
                    base["reason_codes"].extend(report.get("reason_codes", []))
                    if report and report.get("status") != "preflight_passed":
                        base["status"] = "partially_ready"
                if trust_rc:
                    base["status"] = "blocked"
                    base["reason_codes"].extend(
                        e.get("reason_code", "TRUST_PREFLIGHT_FAILED")
                        for e in trust_events
                        if e.get("failed")
                    )
        if not rc and operation == "install":
            for report in reports:
                if report.get("trusted_ca_pem") and c.get("handoff"):
                    folder = private_dir(handoff.state_directory(c))
                    atomic(folder / "trusted-ca.pem", report["trusted_ca_pem"])
        if not rc and operation == "trust":
            base["status"] = (
                "trust_configured"
                if reports and all(r.get("trust_activated") for r in reports)
                else "partially_ready"
            )
        if not rc and operation in ("install", "trust"):
            try:
                cluster.collect(c)
            except Exception:
                raise HarborError("POST_CHANGE_NODE_VALIDATION_FAILED") from None
        if not rc and operation == "verify":
            from .verification import verify

            verification = verify(c, values)
            base["checks"].extend(verification["checks"])
            base["status"] = verification["status"]
            base["reason_codes"].extend(verification["reason_codes"])
        if base["reason_codes"] and base["status"] in (
            "configured",
            "trust_configured",
            "ready",
        ):
            base["status"] = "partially_ready"
    except HarborError as exc:
        base.update(
            status="blocked",
            changed_unknown=operation in ("install", "configure", "trust"),
            reason_codes=[*base["reason_codes"], exc.code],
        )
    finally:
        base["finished_at"] = datetime.now(timezone.utc).isoformat()
    base["reason_codes"] = sorted(set(base["reason_codes"]))
    return base
