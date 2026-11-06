"""Execute packaged Ansible in private workspaces; only publish allowlisted JSON."""

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

from .config import ASSETS
from .discovery import aggregate


def private_json(path, obj):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".kc-result-", delete=False
        ) as f:
            temporary = f.name
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def inventory(config, operation):
    hosts = {}
    for index, node in enumerate(config.get("nodes", [])):
        if (
            operation in ("preflight", "install", "verify")
            and node["name"] != config["install"]["node_name"]
        ):
            continue
        vars_ = {
            "ansible_host": node["ssh_host"],
            "ansible_port": node["ssh_port"],
            "ansible_user": node["ssh_user"],
            "ansible_ssh_private_key_file": node["private_key_file"],
            "ansible_ssh_common_args": "-o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile="
            + shlex.quote(node["known_hosts_file"]),
            "ansible_become": True,
            "node_name": node["name"],
            "node_uid": node["uid"],
            "ansible_python_interpreter": "/usr/bin/python3",
            "ansible_ssh_timeout": 15,
        }
        if node.get("become_password_env"):
            # Become passwords are opaque, not interpolated into templates or shell.
            password = os.environ.get(node["become_password_env"])
            if password:
                vars_["ansible_become_password"] = password
        hosts[f"node_{index}"] = vars_
    return {
        "all": {
            "children": {
                "keycloak_nodes": {"hosts": hosts},
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


def run(operation, config):
    executable = shutil.which("ansible-playbook")
    if executable is None:
        result = aggregate(
            operation,
            config,
            {},
            [{"reason_code": "ANSIBLE_NOT_INSTALLED", "host": "controller"}],
            1,
        )
        return result
    started = datetime.now(UTC).isoformat()
    with tempfile.TemporaryDirectory(prefix="keycloak-infra-") as directory:
        work = Path(directory)
        reports_dir = work / "reports"
        reports_dir.mkdir(mode=0o700)
        private_json(work / "inventory.json", inventory(config, operation))
        queries = [{"api_version": "v1", "kind": "Node"}]
        if operation == "discover":
            for namespace in config["cluster"]["namespaces"] or [None]:
                for kind, version in (
                    ("Pod", "v1"),
                    ("Service", "v1"),
                    ("Deployment", "apps/v1"),
                    ("ReplicaSet", "apps/v1"),
                    ("StatefulSet", "apps/v1"),
                    ("Ingress", "networking.k8s.io/v1"),
                ):
                    queries.append(
                        {
                            "api_version": version,
                            "kind": kind,
                            **({"namespace": namespace} if namespace else {}),
                        }
                    )
        private_json(
            work / "vars.json",
            {
                "kc": config,
                "kc_report_dir": str(reports_dir),
                "kc_operation": operation,
                "kc_queries": queries,
            },
        )
        env = dict(os.environ)
        env.update(
            {
                "ANSIBLE_CONFIG": str(ASSETS / "ansible.cfg"),
                "ANSIBLE_LIBRARY": str(ASSETS / "library"),
                "ANSIBLE_MODULE_UTILS": str(ASSETS / "module_utils"),
                "ANSIBLE_FILTER_PLUGINS": str(ASSETS / "filter_plugins"),
                "ANSIBLE_ROLES_PATH": str(ASSETS / "roles"),
                "ANSIBLE_CALLBACK_PLUGINS": str(ASSETS / "callback_plugins"),
                "ANSIBLE_CALLBACKS_ENABLED": "keycloak_events",
                "ANSIBLE_STDOUT_CALLBACK": "default",
                "ANSIBLE_LOG_PATH": os.devnull,
                "ANSIBLE_HOME": str(work / "home"),
                "ANSIBLE_COLLECTIONS_PATH": env.get(
                    "ANSIBLE_COLLECTIONS_PATH",
                    os.path.expanduser("~/.ansible/collections")
                    + os.pathsep
                    + "/usr/share/ansible/collections",
                ),
                "ANSIBLE_LOCAL_TEMP": str(work / "local"),
                "ANSIBLE_HOST_KEY_CHECKING": "True",
                "ANSIBLE_DISPLAY_ARGS_TO_STDOUT": "False",
                "KC_EVENTS_FILE": str(work / "events.json"),
                "KC_CHANGES_FILE": str(work / "changes.json"),
                "PYTHONPATH": os.pathsep.join(sys.path),
            }
        )
        command = [
            executable,
            "-i",
            str(work / "inventory.json"),
            str(ASSETS / "playbooks" / f"{operation}.yml"),
            "--extra-vars",
            "@" + str(work / "vars.json"),
        ]
        # Never relay Ansible's raw messages: failed modules may contain secrets.
        # A private file avoids memory growth on large discoveries.
        events = []
        with (work / "ansible-output").open("wb") as log:
            proc = subprocess.Popen(
                command,
                cwd=ASSETS,
                env=env,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=config["timeout_seconds"])
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                code = 1
                events.append(
                    {
                        "reason_code": "EXECUTION_TIMEOUT_REMOTE_STATE_UNKNOWN",
                        "host": "controller",
                    }
                )
            except KeyboardInterrupt:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait()
                code = 1
                events.append(
                    {
                        "reason_code": "INTERRUPTED_REMOTE_STATE_UNKNOWN",
                        "host": "controller",
                    }
                )
        events_file = work / "events.json"
        if events_file.exists():
            events.extend(json.loads(events_file.read_text()))
        reports = {}
        for path in reports_dir.glob("*.json"):
            reports[path.stem] = json.loads(path.read_text())
        result = aggregate(operation, config, reports, events, code)
        result["changes"] = (
            json.loads((work / "changes.json").read_text())
            if (work / "changes.json").exists()
            else []
        )
        result.update(
            {
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "ansible_returncode": code,
            }
        )
        return result
