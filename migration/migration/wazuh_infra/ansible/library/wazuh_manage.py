#!/usr/bin/python
"""Manage only this installation. Original-transfer executables are never run."""

import json
import os
import socket
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.wazuh_common import (
    SERVICES,
    STATE,
    WazuhError,
    api,
    check,
    command,
    marker,
    no_symlinks,
    read,
    save,
    storage_identity,
)


def index_api(path, method="GET", body=None, auth=None):
    root = STATE / "pki"
    return api(
        "https://127.0.0.1:9200/" + path.lstrip("/"),
        root / "ca.pem",
        method,
        body,
        auth=auth,
        cert=None if auth else (root / "oneport-admin.pem", root / "oneport-admin.key"),
    )


def manager_security(value):
    ca = STATE / "pki/ca.pem"
    code, data = api(
        "https://127.0.0.1:55000/security/user/authenticate",
        ca,
        "POST",
        auth=("wazuh", value["api_admin"]),
    )
    if code != 200:
        code, data = api(
            "https://127.0.0.1:55000/security/user/authenticate",
            ca,
            "POST",
            auth=("wazuh", "wazuh"),
        )
    check(
        code == 200 and data.get("data", {}).get("token"),
        "MANAGER_API_AUTHENTICATION_FAILED",
    )
    token = data["data"]["token"]
    code, data = api("https://127.0.0.1:55000/security/users", ca, bearer=token)
    check(code == 200, "MANAGER_API_USERS_UNAVAILABLE")
    users = data.get("data", {}).get("affected_items", [])
    for username, key in (("wazuh-wui", "api"), ("wazuh", "api_admin")):
        user = next((u for u in users if u.get("username") == username), None)
        check(user is not None, "MANAGER_API_USER_MISSING")
        code, _ = api(
            "https://127.0.0.1:55000/security/users/" + str(user["id"]),
            ca,
            "PUT",
            {"password": value[key]},
            bearer=token,
        )
        check(code == 200, "MANAGER_API_PASSWORD_CONFIGURATION_FAILED")


def verify(c, probe):
    checks = []
    for package, expected in (
        ("wazuh-indexer", "4.14.10-1"),
        ("wazuh-manager", "4.14.10-1"),
        ("wazuh-dashboard", "4.14.10-1"),
        ("filebeat", "7.10.2"),
    ):
        check(
            command(["dpkg-query", "-W", "-f=${Version}", package]) == expected,
            "PACKAGE_VERSION_MISMATCH",
        )
    for service in SERVICES:
        active = command(["systemctl", "is-active", service]) == "active"
        checks.append(
            {"name": "service:" + service, "status": "pass" if active else "fail"}
        )
    value = read(STATE / "credentials.json")
    code, data = index_api("_cluster/health")
    check(code == 200 and data.get("status") == "green", "INDEXER_NOT_GREEN")
    checks.append({"name": "indexer_health", "status": "pass"})
    code, _ = index_api(
        "wazuh-archives-*/_search?allow_no_indices=true",
        "POST",
        {"size": 0},
        auth=("oneport_reader", value["reader"]),
    )
    check(code == 200, "READER_SEARCH_FAILED")
    checks.append({"name": "reader_search", "status": "pass"})
    code, _ = index_api(
        "_cluster/settings",
        "PUT",
        {"persistent": {}},
        auth=("oneport_reader", value["reader"]),
    )
    check(code == 403, "READER_PRIVILEGE_TOO_BROAD")
    checks.append({"name": "reader_admin_denied", "status": "pass"})
    raw = no_symlinks(c["storage"]["raw_path"])
    command(["runuser", "-u", "oneport-archive", "--", "test", "-w", str(raw)])
    command(
        [
            "runuser",
            "-u",
            "wazuh-indexer",
            "--",
            "test",
            "-w",
            c["storage"]["index_path"],
        ]
    )
    with tempfile.NamedTemporaryFile(dir=raw, prefix=".wi-probe-", delete=True) as f:
        f.write(b"oneport-storage-probe")
        f.flush()
        os.fsync(f.fileno())
    checks.append({"name": "raw_storage_writable", "status": "pass"})
    if probe:
        token = "wi-probe-" + uuid.uuid4().hex
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect("/var/ossec/queue/sockets/queue")
            sock.send(
                (
                    "1:oneport-wazuh-probe:" + json.dumps({"oneport_probe": token})
                ).encode()
            )
        found = False
        for _ in range(20):
            code, response = index_api(
                "wazuh-archives-*/_search?allow_no_indices=true",
                "POST",
                {"size": 0, "query": {"match_phrase": {"full_log": token}}},
            )
            total = response.get("hits", {}).get("total", {})
            if (
                code == 200
                and (total.get("value", 0) if isinstance(total, dict) else total) > 0
            ):
                found = True
                break
            time.sleep(1)
        check(found, "ARCHIVE_EVENT_NOT_SEARCHABLE")
        checks.append({"name": "non_alert_event_search", "status": "pass"})
    else:
        checks.append({"name": "non_alert_event_search", "status": "skipped"})
    code, failures = index_api("wazuh-ingest-failures*/_count?allow_no_indices=true")
    checks.append(
        {
            "name": "ingestion_quarantine_empty",
            "status": "pass"
            if code == 200 and failures.get("count") == 0
            else "unknown",
            "count": failures.get("count"),
        }
    )
    return checks


def main():
    m = AnsibleModule(
        argument_spec={
            "action": {"type": "str", "required": True},
            "config": {"type": "dict", "required": True},
            "digest": {"type": "str", "required": True},
            "credentials": {"type": "dict", "default": {}, "no_log": True},
        },
        supports_check_mode=False,
    )
    c, action = m.params["config"], m.params["action"]
    report = {"kind": action, "checks": [], "reason_codes": [], "changed": False}
    changed = False
    try:
        if action == "control":
            import fcntl

            control_lock = open(STATE / "control.lock", "a")
            fcntl.flock(control_lock, fcntl.LOCK_EX)
        if action == "claim":
            path = no_symlinks(STATE)
            if not path.exists():
                path.mkdir(mode=0o700)
                save(
                    path / "installation.json",
                    {
                        "installation_id": c["target"]["installation_id"],
                        "machine_id": Path("/etc/machine-id").read_text().strip(),
                        "digest": m.params["digest"],
                        "installed": False,
                        "enabled": False,
                        "storage_identities": {
                            "raw": storage_identity(c["storage"]["raw_path"]),
                            "index": storage_identity(c["storage"]["index_path"]),
                            "intermediate": storage_identity("/var/ossec"),
                        },
                    },
                )
                changed = True
        state = marker(c, m.params["digest"])
        if action == "material":
            from ansible.module_utils.wazuh_material import (
                credentials,
                write_aws_credentials,
                write_configuration,
            )

            value = credentials(m.params["credentials"])
            if not state.get("material_ready"):
                write_configuration(
                    c, value, m.params["credentials"], state.get("enabled", False)
                )
                state["material_ready"] = True
                save(STATE / "installation.json", state)
                changed = True
            else:
                if c["tls"]["mode"] == "provided":
                    check(
                        Path("/etc/wazuh-dashboard/certs/dashboard.pem").read_text()
                        == m.params["credentials"]["tls"]["certificate_ref"],
                        "TLS_ROTATION_REQUIRES_EXPLICIT_PLAN",
                    )
                changed = write_aws_credentials(c, m.params["credentials"])
        elif action == "security" and not state.get("security_ready"):
            root = STATE / "pki"
            command(
                [
                    "/usr/share/wazuh-indexer/plugins/opensearch-security/tools/securityadmin.sh",
                    "-cd",
                    "/etc/wazuh-indexer/opensearch-security",
                    "-icl",
                    "-h",
                    "localhost",
                    "-p",
                    "9200",
                    "-cacert",
                    str(root / "ca.pem"),
                    "-cert",
                    str(root / "oneport-admin.pem"),
                    "-key",
                    str(root / "oneport-admin.key"),
                ],
                timeout=120,
                env={
                    **os.environ,
                    "JAVA_HOME": "/usr/share/wazuh-indexer/jdk",
                    "OPENSEARCH_CONF_DIR": "/etc/wazuh-indexer",
                },
            )
            template = read("/etc/filebeat/wazuh-template.json")
            template["settings"].update(
                {
                    "index.number_of_shards": "1",
                    "index.number_of_replicas": "0",
                    "index.auto_expand_replicas": "false",
                }
            )
            code, _ = index_api("_template/wazuh", "PUT", template)
            check(code in (200, 201), "INDEX_TEMPLATE_CONFIGURATION_FAILED")
            bootstrap_index = "wazuh-archives-4.x-" + datetime.now(UTC).strftime(
                "%Y.%m.%d"
            )
            code, _ = index_api(bootstrap_index, "HEAD")
            if code == 404:
                code, _ = index_api(bootstrap_index, "PUT", {})
                check(code in (200, 201), "ARCHIVE_BOOTSTRAP_INDEX_FAILED")
            else:
                check(code == 200, "ARCHIVE_BOOTSTRAP_INDEX_FAILED")
            manager_security(read(STATE / "credentials.json"))
            from ansible.module_utils.wazuh_ingestion import FAILURE_INDEX, simulation

            code, _ = index_api(
                "_template/oneport-ingest-failures",
                "PUT",
                {
                    "index_patterns": [FAILURE_INDEX],
                    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                    "mappings": {
                        "dynamic": False,
                        "properties": {
                            "oneport_original_message": {"type": "text"},
                            "oneport_ingest_error": {"type": "text"},
                            "oneport_failure_time": {"type": "date"},
                        },
                    },
                },
            )
            check(code in (200, 201), "QUARANTINE_TEMPLATE_CONFIGURATION_FAILED")
            code, simulated = index_api(
                "_ingest/pipeline/_simulate", "POST", simulation()
            )
            simulated_doc = simulated.get("docs", [{}])[0].get("doc", {})
            check(
                code == 200
                and simulated_doc.get("_index") == FAILURE_INDEX
                and simulated_doc.get("_source", {}).get("oneport_original_message")
                == "oneport-invalid-json-probe",
                "QUARANTINE_PIPELINE_SIMULATION_FAILED",
            )
            state["security_ready"] = True
            save(STATE / "installation.json", state)
            changed = True
        elif action == "finalize":
            check(state.get("security_ready"), "SECURITY_NOT_INITIALIZED")
            value = read(STATE / "credentials.json")
            url = (
                f"https://{c['target']['bind_address']}:{c['target']['dashboard_port']}"
            )
            code, _ = api(
                url
                + "/api/saved_objects/index-pattern/oneport-archives?overwrite=true",
                STATE / "dashboard-ca.pem",
                "POST",
                {
                    "attributes": {
                        "title": "wazuh-archives-*",
                        "timeFieldName": "timestamp",
                    }
                },
                auth=("oneport_operator", value["operator"]),
                extra_headers={"osd-xsrf": "true"},
            )
            check(code in (200, 201), "DASHBOARD_ARCHIVE_PATTERN_FAILED")
            report["checks"] = verify(c, False)
            # End-to-end probe belongs to explicit verify; it does not block package installation.
            report["checks"] = [
                x for x in report["checks"] if x["name"] != "non_alert_event_search"
            ]
            changed = not state.get("installed")
            state["installed"] = True
            save(STATE / "installation.json", state)
        elif action == "verify":
            report["checks"] = verify(c, c["verification"]["probe"])
        elif action == "control":
            from ansible.module_utils.wazuh_material import configure_manager

            check(state.get("installed"), "INSTALLATION_NOT_COMPLETE")
            requested = c["control"]["action"]
            xml = ET.fromstring(
                "<root>" + Path("/var/ossec/etc/ossec.conf").read_text() + "</root>"
            )
            inputs = xml.findall(".//wodle[@name='aws-s3']/disabled")
            check(
                len(inputs) == 1 and inputs[0].text in ("yes", "no"),
                "SOURCE_CONFIGURATION_UNCONFIRMED",
            )
            observed_enabled = inputs[0].text == "no"
            if requested != "status":
                enable = requested == "start"
                if enable:
                    check(bool(c["sources"]), "NO_SOURCES_SELECTED")
                    from ansible.module_utils.wazuh_host_helpers import (
                        assess_sources,
                        storage_safe,
                    )

                    check(storage_safe(c), "STORAGE_GUARD_BLOCKED")
                    source_results = assess_sources(c, m.params["credentials"]["aws"])
                    check(
                        all(s["readable"] for s in source_results),
                        "SOURCE_ACCESS_UNCONFIRMED",
                    )
                if (
                    enable != observed_enabled
                    or enable != state.get("enabled", False)
                    or state.get("control_intent")
                ):
                    state["control_intent"] = requested
                    save(STATE / "installation.json", state)
                    configure_manager(c, enable)
                    command(["/var/ossec/bin/wazuh-modulesd", "-t"])
                    command(["systemctl", "restart", "wazuh-manager"], timeout=120)
                    state["enabled"] = enable
                    state.pop("control_intent", None)
                    if enable:
                        state.pop("pause_reason", None)
                    save(STATE / "installation.json", state)
                    changed = True
                    observed_enabled = enable
            settled = not state.get("control_intent") and observed_enabled == state.get(
                "enabled", False
            )
            report["ingestion_enabled"] = observed_enabled if settled else None
            if not settled:
                report["reason_codes"].append("CONTROL_STATE_UNCONFIRMED")
                report["checks"].append({"name": "control_state", "status": "unknown"})
            report["ingestion_health"] = "not_observed"
            report["pause_reason"] = state.get("pause_reason")
            report["external_transfer_stop_required"] = (
                state.get("pause_reason") == "STORAGE_GUARD_BLOCKED"
            )
            report["stop_scope"] = "all_sources_owned_by_this_manager"
            report["per_source_pause_supported"] = False
        if action in ("finalize", "verify"):
            report["readiness"] = {
                "infrastructure_ready": bool(state.get("installed")),
                "raw_storage_ready": True,
                "search_ingestion_ready": any(
                    x["name"] == "non_alert_event_search" and x["status"] == "pass"
                    for x in report["checks"]
                ),
                "source_access_ready": False,
                "raw_transfer_verified": False,
                "keycloak_configured": False,
            }
            report["raw_archive_contract"] = {
                "schema_version": 1,
                "root": c["storage"]["raw_path"],
                "manifest_path": "/var/lib/oneport-wazuh-archive-manifests",
                "owner": "oneport-archive",
                "budget_bytes": c["storage"]["raw_budget_bytes"],
                "download_implemented": False,
            }
            report["trusted_ca_pem"] = (STATE / "dashboard-ca.pem").read_text()
            report["dashboard_url"] = (
                f"https://{c['target']['bind_address']}:{c['target']['dashboard_port']}"
            )
        report["changed"] = changed
        m.exit_json(changed=changed, report=report)
    except WazuhError as exc:
        report["reason_codes"] = [str(exc)]
        report["checks"].append({"name": action, "status": "fail"})
    except Exception:
        report["reason_codes"] = ["MANAGED_OPERATION_FAILED"]
        report["checks"].append({"name": action, "status": "unknown"})
    m.fail_json(msg="WAZUH_OPERATION_FAILED", changed=changed, report=report)


if __name__ == "__main__":
    main()
