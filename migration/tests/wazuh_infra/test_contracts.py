import copy
import json
import subprocess
import sys

import pytest
import yaml

from migration.wazuh_infra.archive import validate_manifest
from migration.wazuh_infra.capacity import calculate
from migration.wazuh_infra.config import (
    ConfigError,
    configuration_digest,
    secret,
    validate,
)
from migration.wazuh_infra.runner import aggregate, inventory


@pytest.mark.parametrize(
    "operation",
    ["discover", "assess", "preflight", "install", "configure", "verify", "control"],
)
def test_valid_platform_input(raw_config, operation):
    assert validate(raw_config, operation)["request_id"] == "request-1"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(password="never-echo"),
        lambda c: c["target"].update(bind_address="0.0.0.0"),
        lambda c: c["target"].update(node_name="wrong-host"),
        lambda c: c["target"].update(dashboard_port=True),
        lambda c: c.update(storage={"raw_path": "/srv/../etc"}),
        lambda c: c.update(
            storage={"raw_path": "/srv/data", "index_path": "/srv/data/index"}
        ),
        lambda c: c.update(storage={"index_factor_low": 3, "index_factor_high": 1}),
        lambda c: c.update(control={"action": "delete"}),
        lambda c: c["nodes"].append(copy.deepcopy(c["nodes"][0])),
        lambda c: c.update(verification={"probe": "false"}),
    ],
)
def test_reject_invalid_or_unsafe_requests_without_values(raw_config, mutate):
    mutate(raw_config)
    with pytest.raises(ConfigError) as error:
        validate(raw_config, "install")
    assert "never-echo" not in str(error.value)


def test_credentials_reject_symlinks_and_public_files(tmp_path):
    p = tmp_path / "private"
    p.write_text("sensitive")
    p.chmod(0o644)
    with pytest.raises(ConfigError):
        secret(str(p))
    p.chmod(0o600)
    assert secret(str(p)) == "sensitive"
    alias = tmp_path / "alias"
    alias.symlink_to(p)
    with pytest.raises(ConfigError):
        secret(str(alias))


def test_inventory_targets_one_host_and_checks_known_host(raw_config):
    c = validate(raw_config, "install")
    other = copy.deepcopy(c["nodes"][0])
    other.update(name="worker-2", uid="uid-2")
    c["nodes"].append(other)
    hosts = inventory(c, "install")["all"]["children"]["wazuh_nodes"]["hosts"]
    assert len(hosts) == 1
    assert (
        "StrictHostKeyChecking=yes"
        in next(iter(hosts.values()))["ansible_ssh_common_args"]
    )


def test_digest_ignores_request_but_refuses_source_window_changes(raw_config):
    c = validate(raw_config, "install")
    before = configuration_digest(c)
    c["request_id"] = "request-2"
    c["control"]["action"] = "start"
    assert configuration_digest(c) == before
    c["storage"]["raw_budget_bytes"] += 1
    assert configuration_digest(c) != before


def filesystems(device="disk-1", available=1000):
    return {
        name: {"device": device, "available_bytes": available, "path": "/" + name}
        for name in ("raw", "index", "intermediate")
    }


def storage():
    return {
        "raw_budget_bytes": 200,
        "index_budget_bytes": 300,
        "intermediate_budget_bytes": 100,
        "reserve_bytes": 100,
    }


def test_shared_device_space_is_not_multiplied():
    result = calculate(filesystems(available=650), storage())
    assert len(result["devices"]) == 1
    assert result["devices"]["disk-1"]["remaining_bytes"] == -50
    assert not result["budget_fits"]


def test_different_devices_each_retain_their_reserve():
    fs = filesystems()
    fs["index"] = {"device": "disk-2", "available_bytes": 350, "path": "/index"}
    assert not calculate(fs, storage())["budget_fits"]


def test_compressed_object_size_is_not_an_index_size_estimate():
    result = calculate(
        filesystems(), storage(), observed_source_bytes=40, coverage_complete=True
    )
    assert result["source_raw_bytes"] == 40
    assert result["estimated_need"] is None
    assert result["all_selected_logs_fit"] is None
    assert result["estimated_search_retention_days"] is None


def test_partial_listing_cannot_become_a_total():
    result = calculate(
        filesystems(), storage(), observed_source_bytes=40, coverage_complete=False
    )
    assert result["source_raw_bytes"] is None
    assert result["source_size_basis"] == "unknown"


def test_explicit_size_factors_produce_conditional_estimate():
    s = {
        **storage(),
        "source_raw_bytes": 40,
        "decoded_bytes": 100,
        "index_factor_low": 1,
        "index_factor_high": 2.5,
        "daily_raw_bytes": 10,
    }
    result = calculate(filesystems(), s)
    assert result["estimated_need"]["index_high_bytes"] == 250
    assert result["all_selected_logs_fit"] is True
    assert result["estimated_raw_retention_days"] == 20


def test_existing_managed_data_is_not_charged_twice():
    fs = filesystems()
    fs["raw"]["managed_used_bytes"] = 150
    fs["index"]["managed_used_bytes"] = 200
    fs["intermediate"]["managed_used_bytes"] = 50
    result = calculate(fs, storage())
    assert result["devices"]["disk-1"]["allocated_bytes"] == 200
    assert result["devices"]["disk-1"]["remaining_bytes"] == 700


def test_pause_does_not_need_expired_or_removed_aws_credentials(raw_config, tmp_path):
    raw_config["sources"] = [
        {
            "id": "s1",
            "type": "cloudtrail",
            "bucket": "example-logs",
            "account_id": "123456789012",
            "region": "ap-northeast-2",
            "credentials_file": str(tmp_path / "removed-credentials"),
        }
    ]
    raw_config["control"] = {"action": "pause"}
    raw_config["access"] = {}
    assert validate(raw_config, "control")["control"]["action"] == "pause"
    raw_config["control"]["action"] = "start"
    with pytest.raises(ConfigError):
        validate(raw_config, "control")


def test_overlapping_source_prefixes_do_not_produce_a_false_total(raw_config):
    c = validate(raw_config, "assess")
    c["sources"] = [
        {"type": "cloudtrail", "bucket": "logs", "prefix": "AWSLogs/"},
        {"type": "config", "bucket": "logs", "prefix": "AWSLogs/123/"},
    ]
    report = {
        "filesystems": filesystems(available=100 * 1024**3),
        "sources": [
            {"listing_complete": True, "observed_bytes": 100},
            {"listing_complete": True, "observed_bytes": 100},
        ],
        "checks": [],
    }
    result = aggregate(c, "assess", [report], 0, "test")
    assert result["capacity"]["source_raw_bytes"] is None
    assert "OVERLAPPING_SOURCE_SCOPES" in result["capacity"]["reason_codes"]


def test_supplied_config_and_results_match_documented_schemas(raw_config):
    from pathlib import Path

    import jsonschema

    docs = Path(__file__).parents[2] / "docs/wazuh-infra"
    jsonschema.validate(
        raw_config, json.loads((docs / "input.schema.json").read_text())
    )
    result = aggregate(validate(raw_config, "verify"), "verify", [], 1, "test")
    jsonschema.validate(result, json.loads((docs / "result.schema.json").read_text()))


def test_external_archive_contract_does_not_claim_search_completion():
    record = {
        "schema_version": 1,
        "source_id": "source-1",
        "object_key": "AWSLogs/file.json.gz",
        "local_relative_path": "source-1/object.json.gz",
        "bytes": 10,
        "sha256": "a" * 64,
        "archived_at": "2026-11-07T00:00:00Z",
        "verification": "local_only",
    }
    assert validate_manifest(record)["search_verified"] is False
    with pytest.raises(ValueError):
        validate_manifest({**record, "local_relative_path": "../../etc/shadow"})
    with pytest.raises(ValueError):
        validate_manifest({**record, "verification": "assumed"})


def test_failed_or_unexecuted_checks_never_claim_full_readiness(raw_config):
    c = validate(raw_config, "verify")
    result = aggregate(
        c, "verify", [{"checks": [{"name": "search", "status": "skipped"}]}], 0, "test"
    )
    assert result["status"] == "partial"
    assert result["full_migration_verified"] is False
    assert result["archive_integration_status"] == "external_dependency_pending"
    result = aggregate(c, "install", [], 1, "test", "REMOTE_STATE_UNKNOWN")
    assert result["status"] == "failed"
    assert result["changes_may_be_incomplete"] is True


def test_cli_error_is_single_json_without_submitted_values(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump({"schema_version": 1, "password": "do-not-echo-this"}))
    p.chmod(0o600)
    proc = subprocess.run(
        [sys.executable, "-m", "migration.wazuh_infra", "install", "--config", str(p)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["status"] == "blocked"
    assert "do-not-echo-this" not in proc.stdout + proc.stderr
