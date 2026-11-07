"""Run packaged Ansible without exposing its raw output or credentials."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .capacity import calculate
from .config import ASSETS, configuration_digest, secret


def private_json(path, value):
    with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
        json.dump(value, f)


def inventory(c, operation):
    hosts = {}
    for i, n in enumerate(c["nodes"]):
        if operation != "discover" and n["name"] != c["target"].get("node_name"):
            continue
        v = {
            "ansible_host": n["ssh_host"],
            "ansible_port": n["ssh_port"],
            "ansible_user": n["ssh_user"],
            "ansible_ssh_private_key_file": n["private_key_file"],
            "ansible_ssh_common_args": "-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="
            + shlex.quote(n["known_hosts_file"]),
            "ansible_become": True,
            "ansible_python_interpreter": "/usr/bin/python3",
            "wi_node_name": n["name"],
            "wi_node_uid": n["uid"],
            "ansible_ssh_timeout": 15,
        }
        if n.get("become_password_ref"):
            v["ansible_become_password"] = secret(n["become_password_ref"]).strip()
        hosts[f"node_{i}"] = v
    return {
        "all": {
            "children": {
                "wazuh_nodes": {"hosts": hosts},
                "controllers": {
                    "hosts": {
                        "localhost": {
                            "ansible_connection": "local",
                            "ansible_python_interpreter": sys.executable,
                        }
                    }
                },
            }
        }
    }


def aggregate(c, operation, reports, rc, started, reason=None):
    reasons = sorted({r for x in reports for r in x.get("reason_codes", [])})
    if reason:
        reasons.append(reason)
    checks = [v for x in reports for v in x.get("checks", [])]
    failed = any(v.get("status") == "fail" for v in checks)
    unknown = any(v.get("status") in ("unknown", "skipped") for v in checks)
    status = (
        "failed"
        if rc
        else "blocked"
        if failed
        else "partial"
        if unknown or reasons or not reports
        else "complete"
    )
    result = {
        "schema_version": 1,
        "request_id": c["request_id"],
        "operation": operation,
        "scope": "infrastructure_only",
        "status": status,
        "reason_codes": sorted(set(reasons)),
        "target": {k: c["target"].get(k) for k in ("installation_id", "node_name")},
        "checks": checks,
        "reports": reports,
        "changed": any(x.get("changed", False) for x in reports),
        "changes_may_be_incomplete": bool(rc),
        "archive_integration_status": "external_dependency_pending",
        "full_migration_verified": False,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    if operation == "discover":
        cluster_nodes = {n["name"] for r in reports for n in r.get("nodes", [])}
        host_nodes = {r["node_name"] for r in reports if "node_name" in r}
        if cluster_nodes - host_nodes:
            result["reason_codes"].append("HOST_DISCOVERY_INCOMPLETE")
            result["status"] = "partial" if not rc else "failed"
    fs = next((r["filesystems"] for r in reports if r.get("filesystems")), None)
    if fs:
        sources = [s for r in reports for s in r.get("sources", [])]
        complete = bool(sources) and all(s.get("listing_complete") for s in sources)
        scopes = [
            (s["bucket"], s.get("prefix", ""))
            for s in c["sources"]
            if s["type"] != "cloudwatchlogs"
        ]
        overlaps = any(
            a[0] == b[0] and (a[1].startswith(b[1]) or b[1].startswith(a[1]))
            for i, a in enumerate(scopes)
            for b in scopes[i + 1 :]
        )
        complete = complete and not overlaps
        capacity = calculate(
            fs,
            c["storage"],
            sum(s.get("observed_bytes") or 0 for s in sources),
            complete,
        )
        if overlaps:
            capacity["reason_codes"].append("OVERLAPPING_SOURCE_SCOPES")
        result["capacity"] = capacity
        if not capacity["budget_fits"] and result["status"] == "complete":
            result["status"] = "blocked"
    readiness = next(
        (r.get("readiness") for r in reversed(reports) if "readiness" in r), None
    )
    if readiness:
        result["readiness"] = {
            **readiness,
            "infrastructure_ready": bool(readiness.get("infrastructure_ready"))
            and not rc
            and not failed,
        }
    return result


def run(c, operation):
    started = datetime.now(UTC).isoformat()
    executable = shutil.which("ansible-playbook")
    if executable is None:
        return aggregate(c, operation, [], 1, started, "ANSIBLE_NOT_INSTALLED")
    with tempfile.TemporaryDirectory(prefix="wazuh-infra-") as directory:
        work = Path(directory)
        report_dir = work / "reports"
        report_dir.mkdir(mode=0o700)
        private_json(work / "inventory.json", inventory(c, operation))
        credentials = (
            {k: secret(v).strip() for k, v in c["access"].items()}
            if operation in ("install", "configure")
            else {}
        )
        need_aws = operation in ("install", "configure", "assess") or (
            operation == "control" and c["control"]["action"] == "start"
        )
        credentials["aws"] = (
            {s["id"]: secret(s["credentials_file"]) for s in c["sources"]}
            if need_aws
            else {}
        )
        if c["tls"]["mode"] == "provided" and operation in ("install", "configure"):
            credentials["tls"] = {
                k: secret(c["tls"][k])
                if k == "private_key_ref"
                else Path(c["tls"][k]).read_text()
                for k in ("certificate_ref", "private_key_ref", "ca_ref")
            }
        if c["endpoint"].get("password_ref"):
            credentials["endpoint_password"] = secret(
                c["endpoint"]["password_ref"]
            ).strip()
        private_json(
            work / "vars.json",
            {
                "wi": c,
                "wi_credentials": credentials,
                "wi_digest": configuration_digest(c),
            },
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
        env.update(
            ANSIBLE_CONFIG=str(ASSETS / "ansible.cfg"),
            ANSIBLE_LIBRARY=str(ASSETS / "library"),
            ANSIBLE_MODULE_UTILS=str(ASSETS / "module_utils"),
            ANSIBLE_ACTION_PLUGINS=str(ASSETS / "action_plugins"),
            ANSIBLE_HOME=str(work / "home"),
            ANSIBLE_LOCAL_TEMP=str(work / "tmp"),
            WI_REPORT_DIR=str(report_dir),
        )
        rc, reason = 1, None
        with (work / "ansible.log").open("wb") as output:
            process = subprocess.Popen(
                [
                    executable,
                    "-i",
                    str(work / "inventory.json"),
                    "-e",
                    "@" + str(work / "vars.json"),
                    str(ASSETS / "playbooks" / (operation + ".yml")),
                ],
                stdout=output,
                stderr=output,
                env=env,
                start_new_session=True,
            )
            try:
                rc = process.wait(timeout=c["timeout_seconds"])
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                reason = "REMOTE_STATE_UNKNOWN"
        reports = []
        for p in sorted(report_dir.glob("*.json")):
            reports.append(json.loads(p.read_text()))
        if rc and not reason:
            reason = "ANSIBLE_EXECUTION_FAILED"
        return aggregate(c, operation, reports, rc, started, reason)
