#!/usr/bin/python3
"""Pause this manager's AWS input when storage budgets are exhausted. Never delete."""

import fcntl
import xml.etree.ElementTree as ET
from pathlib import Path

from wazuh_common import STATE, atomic, command, read, save
from wazuh_host_helpers import storage_safe


def pause(config):
    state = read(STATE / "installation.json")
    state.update(control_intent="pause", pause_reason="STORAGE_GUARD_BLOCKED")
    save(STATE / "installation.json", state)
    path = Path("/var/ossec/etc/ossec.conf")
    root = ET.fromstring("<root>" + path.read_text() + "</root>")
    for module in root.findall(".//wodle[@name='aws-s3']"):
        module.find("disabled").text = "yes"
    atomic(
        path, "\n".join(ET.tostring(item, encoding="unicode") for item in root), 0o640
    )
    command(["/var/ossec/bin/wazuh-modulesd", "-t"])
    command(["systemctl", "restart", "wazuh-manager"], timeout=120)
    state.update(enabled=False, pause_reason="STORAGE_GUARD_BLOCKED")
    state.pop("control_intent", None)
    save(STATE / "installation.json", state)


def main():
    with open(STATE / "control.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read(STATE / "installation.json")
        if not state.get("installed") or not state.get("enabled"):
            return
        config = read(STATE / "runtime.json")["config"]
        try:
            safe = storage_safe(config)
        except Exception:
            safe = False
        if not safe:
            pause(config)


if __name__ == "__main__":
    main()
