#!/usr/bin/python
"""Read-only process discovery and exact selected-host preflight."""

import json
import os
import pwd
import shutil
import socket
import subprocess
from pathlib import Path

from ansible.module_utils.bao_common import digest
from ansible.module_utils.basic import AnsibleModule


def run(args):
    try:
        p = subprocess.run(args, capture_output=True, timeout=15, check=False)
        return p.returncode, p.stdout.decode(errors="replace")[:65536]
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def collect(config, expected, action):
    code, out = run(
        ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"]
    )
    units = [
        line.split()[0]
        for line in out.splitlines()
        if line.split() and "openbao" in line.split()[0].lower()
    ]
    processes = []
    for entry in Path("/proc").glob("[0-9]*/exe"):
        try:
            executable = os.readlink(entry)
            if Path(executable).name in ("bao", "openbao"):
                processes.append(
                    {"pid": int(entry.parent.name), "product_confirmed": False}
                )
        except OSError:
            continue
    report = {
        "status": "discovered",
        "host_scanned": True,
        "service_names": units,
        "processes": processes,
        "service_scan_complete": code == 0,
    }
    if action == "discover":
        return report
    errors = []
    release = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                release[key] = value.strip('"')
    except OSError:
        pass
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") not in (
        "22.04",
        "24.04",
        "26.04",
    ):
        errors.append("UNSUPPORTED_UBUNTU_VERSION")
    if not Path("/run/systemd/system").is_dir():
        errors.append("SYSTEMD_REQUIRED")
    machine = Path("/etc/machine-id").read_text().strip()
    if not expected.get("machine_id") or machine != expected["machine_id"]:
        errors.append("NODE_MACHINE_ID_MISMATCH")
    if not expected.get("ready"):
        errors.append("KUBERNETES_NODE_NOT_READY")
    if expected.get("control_plane") and not config["target"]["allow_control_plane"]:
        errors.append("CONTROL_PLANE_REQUIRES_SELECTION")
    if config["target"]["mode"] == "reuse":
        return {
            "status": "blocked" if errors else "preflight_passed",
            "reason_codes": errors,
            "machine_id": machine,
            "node_uid": expected["uid"],
            "owned": False,
        }
    mem = (
        int(
            next(
                line.split()[1]
                for line in Path("/proc/meminfo").read_text().splitlines()
                if line.startswith("MemAvailable:")
            )
        )
        // 1024
    )
    # Leave the requested headroom after reserving the service's 1 GiB ceiling.
    if mem < config["target"]["min_memory_mb"] + 1024:
        errors.append("INSUFFICIENT_MEMORY")
    if not Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        errors.append("CGROUP_V2_REQUIRED")
    if shutil.disk_usage("/var/lib").free // 1048576 < config["target"]["min_disk_mb"]:
        errors.append("INSUFFICIENT_DISK")
    state = Path("/var/lib/openbao-infra") / config["target"]["installation_id"]
    marker = state / "installation.json"
    owned = False
    if marker.exists() and not marker.is_symlink():
        owner = json.loads(marker.read_text())
        owned = (
            owner.get("installation_id") == config["target"]["installation_id"]
            and owner.get("node_uid") == expected["uid"]
            and owner.get("machine_id") == machine
        )
        if not owned:
            errors.append("INSTALLATION_OWNERSHIP_CONFLICT")
        settings = {
            "target": config["target"],
            "endpoint": config["endpoint"]["url"],
            "tls_mode": config["tls"]["mode"],
        }
        if owner.get("settings_digest") != digest(settings):
            errors.append("INSTALLATION_SETTINGS_CHANGED")
    paths = (
        "/etc/openbao",
        "/var/lib/openbao",
        "/var/log/openbao",
        "/etc/systemd/system/openbao.service",
        "/opt/openbao",
        "/etc/logrotate.d/openbao",
    )
    if not owned and (processes or units or any(Path(p).exists() for p in paths)):
        errors.append("EXISTING_INSTALLATION_CONFLICT")
    if not owned:
        try:
            pwd.getpwnam("openbao")
            errors.append("SERVICE_USER_CONFLICT")
        except KeyError:
            pass
        for address, port in (
            (config["target"]["bind_address"], config["target"]["port"]),
            ("127.0.0.1", 8201),
        ):
            sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET)
            try:
                sock.bind((address, port))
            except OSError:
                errors.append("PORT_OR_BIND_CONFLICT")
            finally:
                sock.close()
    # A resume may reuse only this exact service, never an unrelated listener.
    if owned:
        _, exec_start = run(
            ["systemctl", "show", "openbao.service", "--property=ExecStart", "--value"]
        )
        if (
            exec_start
            and "/opt/openbao/2.7.2/bao server -config=/etc/openbao/openbao.hcl"
            not in exec_start
        ):
            errors.append("SERVICE_OWNERSHIP_CONFLICT")
    return {
        "status": "blocked" if errors else "preflight_passed",
        "reason_codes": errors,
        "machine_id": machine,
        "node_uid": expected["uid"],
        "owned": owned,
        "control_plane": expected.get("control_plane", False),
    }


def main():
    m = AnsibleModule(
        argument_spec={
            "action": {"type": "str", "default": "discover"},
            "config": {"type": "dict", "default": {}},
            "expected": {"type": "dict", "default": {}},
        },
        supports_check_mode=True,
    )
    try:
        report = collect(m.params["config"], m.params["expected"], m.params["action"])
        m.exit_json(changed=False, report=report)
    except Exception:  # noqa: BLE001 - boundary must redact provider and credential errors
        m.fail_json(msg="Host inspection failed", reason_code="HOST_INSPECTION_FAILED")


if __name__ == "__main__":
    main()
