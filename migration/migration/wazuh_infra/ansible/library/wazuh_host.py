#!/usr/bin/python
"""Host identity, storage, conflicts and bounded read-only S3 assessment."""

import os
import platform
import socket
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.wazuh_common import (
    SERVICES,
    STATE,
    WazuhError,
    check,
    command,
    marker,
    no_symlinks,
    storage_identity,
)
from ansible.module_utils.wazuh_host_helpers import assess_sources


def filesystem(path):
    p = no_symlinks(path)
    while not p.exists():
        p = p.parent
    st, fs = p.stat(), os.statvfs(p)
    check(p.is_dir(), "STORAGE_PARENT_NOT_DIRECTORY")
    return {
        "path": path,
        "observed_path": str(p),
        "device": str(st.st_dev),
        "storage_identity": storage_identity(p),
        "total_bytes": fs.f_blocks * fs.f_frsize,
        "available_bytes": fs.f_bavail * fs.f_frsize,
        "free_inodes": fs.f_favail,
        "read_only": bool(fs.f_flag & os.ST_RDONLY),
        "quota_checked": False,
    }


def main():
    module = AnsibleModule(
        argument_spec={
            "config": {"type": "dict", "required": True},
            "expected_nodes": {"type": "list", "elements": "dict", "default": []},
            "node_name": {"type": "str", "required": True},
            "node_uid": {"type": "str", "required": True},
            "operation": {"type": "str", "required": True},
            "digest": {"type": "str", "default": ""},
            "credentials": {"type": "dict", "default": {}, "no_log": True},
        },
        supports_check_mode=True,
    )
    c, op = module.params["config"], module.params["operation"]
    report = {
        "kind": "host",
        "node_name": module.params["node_name"],
        "checks": [],
        "reason_codes": [],
    }
    try:
        expected = next(
            (
                n
                for n in module.params["expected_nodes"]
                if n["name"] == module.params["node_name"]
            ),
            None,
        )
        check(
            expected and expected["uid"] == module.params["node_uid"],
            "NODE_IDENTITY_UNCONFIRMED",
        )
        machine = Path("/etc/machine-id").read_text().strip()
        check(
            expected.get("machine_id") and expected["machine_id"] == machine,
            "MACHINE_ID_MISMATCH",
        )
        source_node = next(
            n for n in c["nodes"] if n["name"] == module.params["node_name"]
        )
        check(
            source_node["ssh_host"] in expected["addresses"], "SSH_ADDRESS_NOT_ON_NODE"
        )
        if expected.get("system_uuid"):
            check(
                Path("/sys/class/dmi/id/product_uuid").read_text().strip().lower()
                == expected["system_uuid"].lower(),
                "SYSTEM_UUID_MISMATCH",
            )
        check(expected["ready"], "NODE_NOT_READY")
        report["checks"].append({"name": "node_identity", "status": "pass"})
        report["candidates"] = []
        for name in SERVICES:
            p = __import__("subprocess").run(
                ["systemctl", "show", name, "--property=LoadState", "--value"],
                text=True,
                capture_output=True,
            )
            if p.returncode == 0 and p.stdout.strip() == "loaded":
                report["candidates"].append(
                    {
                        "service": name,
                        "deployment_type": "host_service",
                        "evidence": ["systemd_unit"],
                        "product_confirmed": False,
                    }
                )
        if op == "discover":
            module.exit_json(changed=False, report=report)
        check(
            not expected["control_plane"] or c["target"]["allow_control_plane"],
            "CONTROL_PLANE_NOT_AUTHORIZED",
        )
        report["filesystems"] = {
            "raw": filesystem(c["storage"]["raw_path"]),
            "index": filesystem(c["storage"]["index_path"]),
            "intermediate": filesystem("/var/ossec"),
        }
        owned = (STATE / "installation.json").is_file()
        if owned:
            managed_state = marker(c, module.params["digest"])
            for name, fs in report["filesystems"].items():
                expected_identity = managed_state.get("storage_identities", {}).get(
                    name
                )
                if expected_identity and expected_identity != fs["storage_identity"]:
                    if op == "control" and c["control"]["action"] in (
                        "pause",
                        "status",
                    ):
                        report["reason_codes"].append("STORAGE_MOUNT_CHANGED")
                        report["checks"].append(
                            {"name": "storage_mount:" + name, "status": "unknown"}
                        )
                    else:
                        raise WazuhError("STORAGE_MOUNT_CHANGED")
            for fs in report["filesystems"].values():
                if Path(fs["path"]).exists():
                    fs["managed_used_bytes"] = int(
                        command(["du", "-sb", "--", fs["path"]]).split()[0]
                    )
        report["architecture"] = platform.machine()
        report["cpu_count"] = os.cpu_count()
        mem = {
            line.split(":")[0]: int(line.split()[1])
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith(("MemTotal:", "MemAvailable:"))
        }
        report["memory_available_bytes"] = mem["MemAvailable"] * 1024
        if op == "assess":
            report["sources"] = assess_sources(c, module.params["credentials"])
            report["checks"].extend(
                {"name": "source:" + s["source_id"], "status": s["status"]}
                for s in report["sources"]
            )
            report["reason_codes"].extend(
                s["reason_code"] for s in report["sources"] if s.get("reason_code")
            )
            module.exit_json(changed=False, report=report)
        if op in ("configure", "verify", "control"):
            marker(c, module.params["digest"])
            module.exit_json(changed=False, report=report)
        release = dict(
            line.split("=", 1)
            for line in Path("/etc/os-release").read_text().splitlines()
            if "=" in line
        )
        check(
            release.get("ID", "").strip('"') == "ubuntu"
            and release.get("VERSION_ID", "").strip('"') == "24.04",
            "UNSUPPORTED_OS",
        )
        check(platform.machine() == "x86_64", "UNSUPPORTED_ARCHITECTURE")
        check(Path("/run/systemd/system").is_dir(), "SYSTEMD_REQUIRED")
        check(
            int(Path("/proc/sys/vm/max_map_count").read_text()) >= 262144,
            "VM_MAX_MAP_COUNT_REQUIRES_OPERATOR_CHANGE",
        )
        if not (STATE / "installation.json").exists():
            check(
                report["memory_available_bytes"]
                >= c["target"]["min_available_memory_mb"] * 1024**2,
                "INSUFFICIENT_AVAILABLE_MEMORY",
            )
        check(
            c["target"]["heap_mb"] * 2 < c["target"]["min_available_memory_mb"],
            "HEAP_EXCEEDS_HOST_BUDGET",
        )
        by_device = {}
        for name, fs in report["filesystems"].items():
            check(
                not fs["read_only"] and fs["free_inodes"] > 10000, "STORAGE_UNAVAILABLE"
            )
            v = by_device.setdefault(
                fs["device"], [fs["available_bytes"] - c["storage"]["reserve_bytes"], 0]
            )
            allocation = c["storage"][
                {
                    "raw": "raw_budget_bytes",
                    "index": "index_budget_bytes",
                    "intermediate": "intermediate_budget_bytes",
                }[name]
            ]
            v[1] += max(0, allocation - fs.get("managed_used_bytes", 0))
        check(
            all(a >= b + (0 if owned else 4 * 1024**3) for a, b in by_device.values()),
            "INSUFFICIENT_STORAGE_BUDGET",
        )
        managed = (STATE / "installation.json").is_file()
        if managed:
            marker(c, module.params["digest"])
        else:
            check(not report["candidates"], "EXISTING_INSTALLATION_REQUIRES_INSPECT")
            for path in (
                "/var/ossec",
                "/etc/wazuh-indexer",
                "/etc/wazuh-dashboard",
                "/etc/filebeat",
                "/usr/local/lib/oneport-wazuh",
                "/var/lib/oneport-wazuh-archive-manifests",
                "/usr/share/keyrings/oneport-wazuh.gpg",
                "/etc/apt/sources.list.d/oneport-wazuh.list",
                str(STATE),
                c["storage"]["raw_path"],
                c["storage"]["index_path"],
            ):
                check(not no_symlinks(path).exists(), "UNMANAGED_PATH_CONFLICT")
            for port in (9200, 9300, 55000, c["target"]["dashboard_port"]):
                with socket.socket() as sock:
                    try:
                        sock.bind(("0.0.0.0", port))
                    except OSError:
                        raise WazuhError("PORT_CONFLICT") from None
        check(
            c["target"]["bind_address"] in expected["addresses"],
            "BIND_ADDRESS_NOT_ON_SELECTED_NODE",
        )
        report["managed"] = managed
        report["checks"].append({"name": "host_preflight", "status": "pass"})
        module.exit_json(changed=False, report=report)
    except WazuhError as exc:
        report["checks"].append({"name": "host_preflight", "status": "fail"})
        report["reason_codes"].append(str(exc))
        module.exit_json(changed=False, report=report, blocked=True)
    except Exception:
        report["checks"].append({"name": "host_preflight", "status": "unknown"})
        report["reason_codes"].append("HOST_INSPECTION_FAILED")
        module.exit_json(changed=False, report=report, blocked=True)


if __name__ == "__main__":
    main()
