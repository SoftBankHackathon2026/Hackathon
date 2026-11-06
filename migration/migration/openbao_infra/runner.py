"""Run packaged Ansible in a private workspace and return safe platform results."""

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
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import handoff
from .config import ASSETS, require, secret_file


def credentials(c):
    result = {"app_ca": {}, "app_jwt": {}}
    if c.get("endpoint", {}).get("admin_token_ref"):
        result["admin_token"] = secret_file(c["endpoint"]["admin_token_ref"])
    if c.get("verification", {}).get("operator_token_ref"):
        result["operator_token"] = secret_file(c["verification"]["operator_token_ref"])
    tls = c.get("tls", {})
    if tls.get("mode") == "provided":
        result.update(
            tls_certificate=Path(tls["certificate_ref"]).read_text(),
            tls_private_key=secret_file(tls["private_key_ref"]),
            tls_ca=Path(tls["ca_ref"]).read_text(),
        )
    if c.get("keycloak"):
        result["oidc_client_secret"] = secret_file(c["keycloak"]["client_secret_ref"])
        result["oidc_ca"] = Path(c["keycloak"]["ca_ref"]).read_text()
        if c["keycloak"].get("manage_client"):
            result["keycloak_admin_token"] = secret_file(
                c["keycloak"]["admin_token_ref"]
            )
    for app in c.get("applications", []):
        result["app_ca"][app["name"]] = Path(app["ca_ref"]).read_text()
        if app.get("probe_jwt_ref"):
            result["app_jwt"][app["name"]] = secret_file(app["probe_jwt_ref"])
    return result


def inventory(c, operation):
    hosts = {}
    for index, node in enumerate(c["nodes"]):
        if operation != "discover" and node["name"] != c.get("target", {}).get(
            "node_name"
        ):
            continue
        v = {
            "node_name": node["name"],
            "node_uid": node["uid"],
            "ansible_host": node["ssh_host"],
            "ansible_port": node["ssh_port"],
            "ansible_user": node["ssh_user"],
            "ansible_ssh_private_key_file": node["private_key_file"],
            "ansible_ssh_common_args": "-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="
            + shlex.quote(node["known_hosts_file"]),
            "ansible_python_interpreter": "/usr/bin/python3",
            "ansible_ssh_timeout": 15,
        }
        if node.get("become_password_ref"):
            v["ansible_become_password"] = secret_file(node["become_password_ref"])
        hosts["node_" + str(index)] = v
    return {
        "all": {
            "children": {
                "openbao_nodes": {"hosts": hosts},
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


def aggregate(operation, c, events, exit_code):
    reasons = set()
    reports = []
    for event in events:
        if event.get("failed"):
            reasons.add(event.get("reason_code", "ANSIBLE_TASK_FAILED"))
        report = event.get("report", {})
        if report:
            reports.append({"host": event["host"], **report})
            reasons.update(report.get("reason_codes", []))
    if exit_code:
        reasons.add("ANSIBLE_EXECUTION_FAILED")
    status = "failed" if exit_code else "partially_ready"
    if not exit_code:
        statuses = [r.get("status") for r in reports if "nodes" not in r]
        if operation == "discover":
            status = "discovered"
            cluster = next((r for r in reports if "nodes" in r), {})
            scanned = {n["name"] for n in c["nodes"]}
            if any(n["name"] not in scanned for n in cluster.get("nodes", [])):
                reasons.add("HOST_DISCOVERY_INCOMPLETE")
        elif statuses:
            status = statuses[-1]
        if reasons and operation in ("discover", "configure"):
            status = "partially_ready"
    return {
        "schema_version": 1,
        "request_id": c["request_id"],
        "operation": operation,
        "status": status,
        "reason_codes": sorted(reasons),
        "reports": reports,
        "changed": any(e.get("changed") for e in events),
        "cloud_source": "aws_secrets_manager",
        "secret_transfer_performed": False,
    }


def run(operation, config):
    c = copy.deepcopy(config)
    local_handoff = None
    if operation == "handoff" and c["handoff"]["action"] in ("ack", "cleanup"):
        local_handoff = handoff.complete(c)
        if c["handoff"]["action"] == "cleanup":
            return {
                "schema_version": 1,
                "request_id": c["request_id"],
                "operation": operation,
                **local_handoff,
                "reason_codes": [],
            }
    executable = shutil.which("ansible-playbook")
    if not executable:
        return {
            "schema_version": 1,
            "status": "blocked",
            "reason_codes": ["ANSIBLE_NOT_INSTALLED"],
        }
    with tempfile.TemporaryDirectory(prefix="openbao-infra-") as name:
        work = Path(name)
        os.chmod(work, 0o700)
        resolved = (
            credentials(c)
            if operation not in ("discover", "inspect", "preflight")
            else {}
        )
        remote = copy.deepcopy(c)
        remote["credentials"] = resolved
        remote["remote_ca"] = (
            "/etc/openbao/trust.pem"
            if c.get("tls", {}).get("mode") == "provided"
            else "/etc/openbao/server.crt"
        )
        if c.get("target", {}).get("mode") == "reuse":
            remote["remote_ca"] = (
                "/var/lib/openbao-infra/"
                + c["target"]["installation_id"]
                + "/trust.pem"
            )
            resolved["reuse_ca"] = Path(c["endpoint"]["ca_file"]).read_text()
        if local_handoff:
            remote["handoff_receipt_digest"] = hashlib.sha256(
                Path(c["handoff"]["receipt_path"]).read_bytes()
            ).hexdigest()
        variables = {
            "ob": c,
            "ob_remote": remote,
            "ob_credentials": resolved,
            "ob_operation": operation,
            "ob_work": name,
            "ob_lock_owner": uuid.uuid4().hex,
            "ob_state": "/var/lib/openbao-infra/"
            + c.get("target", {}).get("installation_id", "unused"),
            "ob_endpoint_host": urlsplit(c.get("endpoint", {}).get("url", "")).hostname,
            "ob_releases": json.loads((ASSETS / "releases.json").read_text()),
        }
        if c.get("target") and operation not in ("discover", "inspect"):
            settings = {
                "target": c["target"],
                "endpoint": c["endpoint"]["url"],
                "tls_mode": c["tls"]["mode"],
            }
            variables["ob_settings_digest"] = hashlib.sha256(
                json.dumps(settings, sort_keys=True).encode()
            ).hexdigest()
        handoff.private_json(work / "inventory.json", inventory(c, operation))
        handoff.private_json(work / "vars.json", variables)
        # Ignore user-supplied Ansible plugin/connection overrides, which can exfiltrate variables.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("ANSIBLE_", "OPENBAO_"))
        }
        env.update(
            ANSIBLE_CONFIG=str(ASSETS / "ansible.cfg"),
            ANSIBLE_LIBRARY=str(ASSETS / "library"),
            ANSIBLE_MODULE_UTILS=str(ASSETS / "module_utils"),
            ANSIBLE_CALLBACK_PLUGINS=str(ASSETS / "callback_plugins"),
            ANSIBLE_CALLBACKS_ENABLED="openbao_events",
            ANSIBLE_STDOUT_CALLBACK="default",
            ANSIBLE_LOG_PATH=os.devnull,
            ANSIBLE_HOME=str(work / "home"),
            ANSIBLE_LOCAL_TEMP=str(work / "tmp"),
            ANSIBLE_HOST_KEY_CHECKING="True",
            ANSIBLE_DISPLAY_ARGS_TO_STDOUT="False",
            ANSIBLE_CACHE_PLUGIN="memory",
            OPENBAO_EVENTS_FILE=str(work / "events.jsonl"),
        )
        command = [
            executable,
            "-i",
            str(work / "inventory.json"),
            str(ASSETS / "playbooks" / (operation + ".yml")),
            "--extra-vars",
            "@" + str(work / "vars.json"),
        ]
        with (work / "private-output").open("wb") as output:
            os.chmod(work / "private-output", 0o600)
            proc = subprocess.Popen(
                command,
                cwd=ASSETS,
                env=env,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=c["timeout_seconds"])
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                return {
                    "schema_version": 1,
                    "status": "failed",
                    "reason_codes": ["TIMEOUT_OR_INTERRUPTED"],
                    "pending": [
                        "Inspect target operation lock and partial installation before retrying"
                    ],
                }
        events_path = work / "events.jsonl"
        events = (
            [json.loads(line) for line in events_path.read_text().splitlines()]
            if events_path.exists()
            else []
        )
        result = aggregate(operation, c, events, code)
        if code:
            return result
        if operation == "install":
            # Public trust is returned as public material, not as a soon-deleted temp path.
            anchor = work / "trusted-ca.pem"
            require(anchor.is_file(), "install.trusted_ca")
            result["trusted_ca_pem"] = anchor.read_text()
            result["trusted_ca_sha256"] = hashlib.sha256(
                anchor.read_bytes()
            ).hexdigest()
        if operation == "handoff":
            if c["handoff"]["action"] == "prepare":
                src = work / "recovery-bundle.json"
                os.chmod(src, 0o600)
                result.update(handoff.prepare(c, src))
            else:
                result.update(local_handoff)
        if operation == "issue-migration-token":
            # Platform creates a private destination; ordinary stdout never includes the token.
            out = c.get("handoff", {}).get("directory")
            require(
                out and Path(out).is_dir() and not Path(out).is_symlink(),
                "handoff.directory",
            )
            info = Path(out).stat()
            require(
                info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "handoff.directory.permissions",
            )
            destination = Path(out) / (
                c["target"]["installation_id"] + ".migration.token"
            )
            os.chmod(work / "migration.token", 0o600)
            token = secret_file(str(work / "migration.token"))
            fd, tmp = tempfile.mkstemp(dir=out, prefix=".bao-token-")
            try:
                with os.fdopen(fd, "w") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(token)
                require(not destination.is_symlink(), "credential_ref.symlink")
                os.replace(tmp, destination)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            result["credential_ref"] = str(destination)
            for report in result["reports"]:
                report.pop("credential_remote_ref", None)
        return result
