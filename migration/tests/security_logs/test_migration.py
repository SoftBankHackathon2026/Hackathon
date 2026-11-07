import base64
import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
import yaml

from migration.security_logs import runner
from migration.security_logs.aws import DAY_MS, S3, CloudWatch
from migration.security_logs.config import load
from migration.security_logs.errors import StorageBlocked
from migration.security_logs.search import Search
from migration.security_logs.storage import Archive, locked
from migration.wazuh_infra.archive import validate_manifest
from migration.wazuh_infra.config import ConfigError


@pytest.fixture
def config(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    raw, manifests = tmp_path / "raw", tmp_path / "manifests"
    raw.mkdir(mode=0o700)
    manifests.mkdir(mode=0o700)
    result = tmp_path / "handoff.json"
    result.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation": "verify",
                "status": "partial",
                "reports": [
                    {
                        "readiness": {"raw_storage_ready": True},
                        "raw_archive_contract": {
                            "schema_version": 1,
                            "owner": "oneport-archive",
                            "root": str(raw),
                            "manifest_path": str(manifests),
                            "budget_bytes": 1000000,
                        },
                    }
                ],
            }
        )
    )
    result.chmod(0o600)
    input_path = tmp_path / "config.yaml"
    input_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "wazuh_result": str(result),
                "reserve_bytes": 1,
                "sources": [
                    {
                        "id": "trail",
                        "type": "s3",
                        "account_id": "111111111111",
                        "bucket_owner": "222222222222",
                        "region": "us-east-1",
                        "bucket": "example-logs",
                        "prefix": "trail/",
                    },
                    {
                        "id": "logs",
                        "type": "cloudwatch",
                        "account_id": "111111111111",
                        "region": "us-east-1",
                        "log_group": "/example",
                    },
                ],
            }
        )
    )
    input_path.chmod(0o600)
    old = os.umask(0o077)
    monkeypatch.setattr(runner, "assert_writer", lambda: None)
    try:
        yield load(str(input_path)), input_path
    finally:
        os.umask(old)


@pytest.fixture
def aws():
    boto3 = pytest.importorskip("boto3")
    from botocore.stub import Stubber

    def setup(service):
        client = boto3.client(
            service,
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        stub = Stubber(client)
        stub.activate()
        return client, stub

    return setup


def test_config_rejects_overlapping_s3_scopes_and_unready_handoff(config):
    _, path = config
    raw = yaml.safe_load(path.read_text())
    raw["sources"].append(
        {**raw["sources"][0], "id": "other", "prefix": "trail/nested/"}
    )
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="overlapping_prefix"):
        load(str(path))
    raw["sources"].pop()
    handoff_path = path.with_name("handoff.json")
    handoff = json.loads(handoff_path.read_text())
    handoff["reports"][0]["readiness"]["raw_storage_ready"] = False
    handoff_path.write_text(json.dumps(handoff))
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="raw_archive_contract"):
        load(str(path))


def test_s3_paginated_plan_copy_and_replay_keep_original_bytes(config, aws):
    cfg, _ = config
    source = cfg["sources"][0]
    cfg["sources"] = [source]
    s3, stub = aws("s3")
    provider = S3(s3, source)
    params = {"Bucket": "example-logs", "ExpectedBucketOwner": "222222222222"}
    original = gzip.compress(b'{"Records":[{"eventID":"event-1"}]}')
    object_key = "trail/../../original.gz"
    modified = datetime(2025, 1, 1, tzinfo=UTC)
    item = {
        "Key": object_key,
        "Size": len(original),
        "ETag": '"tag"',
        "LastModified": modified,
    }
    listing = {**params, "Prefix": "trail/"}
    get = {**params, "Key": object_key, "IfMatch": '"tag"'}
    stub.add_response(
        "list_objects_v2",
        {"Contents": [item], "IsTruncated": True, "NextContinuationToken": "page"},
        listing,
    )
    stub.add_response(
        "get_object", {"Body": io.BytesIO(original[:1])}, {**get, "Range": "bytes=0-0"}
    )
    assert provider.plan(1) == {
        "objects_observed": 1,
        "bytes_observed": len(original),
        "listing_complete": False,
        "sample_readable": True,
    }
    for _ in range(2):
        stub.add_response(
            "list_objects_v2",
            {"IsTruncated": True, "NextContinuationToken": "page"},
            listing,
        )
        stub.add_response(
            "list_objects_v2",
            {"Contents": [item], "IsTruncated": False},
            {**listing, "ContinuationToken": "page"},
        )
        if _ == 0:
            stub.add_response(
                "get_object",
                {
                    "Body": io.BytesIO(original),
                    "ContentLength": len(original),
                    "ETag": '"tag"',
                    "LastModified": modified,
                    "VersionId": "v1",
                    "ChecksumType": "FULL_OBJECT",
                    "ChecksumSHA256": base64.b64encode(
                        hashlib.sha256(original).digest()
                    ).decode(),
                },
                {**get, "ChecksumMode": "ENABLED"},
            )

    with locked(cfg, write=True):
        archive = Archive(cfg)
        with archive.connect(write=True):
            cutoff = int(modified.timestamp() * 1000)
            assert not provider.archive(archive, cutoff)["object_failures"]
            assert not provider.archive(archive, cutoff)["object_failures"]
            [(key, record)] = archive.records("trail")
            assert archive.archive_path(record).read_bytes() == original
            assert record["object_key"] == object_key
            assert record["source_version"] == "v1"
            assert validate_manifest(record)["verification"] == "remote_compared"
            assert archive.verify_file(key, record)
    stub.assert_no_pending_responses()


def test_s3_changed_and_archived_objects_keep_scope_incomplete(
    config, aws, monkeypatch
):
    cfg, _ = config
    source = cfg["sources"][0]
    cfg["sources"] = [source]
    s3, stub = aws("s3")
    params = {"Bucket": "example-logs", "ExpectedBucketOwner": "222222222222"}
    stub.add_response(
        "list_objects_v2",
        {
            "IsTruncated": False,
            "Contents": [
                {
                    "Key": key,
                    "ETag": '"tag"',
                    "Size": 1,
                    "LastModified": datetime(2025, 1, 1, tzinfo=UTC),
                }
                for key in ("trail/changed", "trail/restore")
            ],
        },
        {**params, "Prefix": "trail/"},
    )
    for key, error in (
        ("trail/changed", "PreconditionFailed"),
        ("trail/restore", "InvalidObjectState"),
    ):
        stub.add_client_error(
            "get_object",
            service_error_code=error,
            expected_params={
                **params,
                "Key": key,
                "IfMatch": '"tag"',
                "ChecksumMode": "ENABLED",
            },
        )
    monkeypatch.setattr(runner, "open_source", lambda source: S3(s3, source))
    result = runner.run(cfg, "run")
    assert not result["archive_complete"]
    assert result["sources"][0]["active_through_ms"]
    assert result["sources"][0]["object_failures"] == {
        "SOURCE_OBJECT_CHANGED": 1,
        "SOURCE_RESTORE_REQUIRED": 1,
    }

    # A failed object disappearing from the next listing cannot turn into success.
    stub.add_response(
        "list_objects_v2", {"IsTruncated": False}, {**params, "Prefix": "trail/"}
    )
    retry = runner.run(cfg, "run")
    assert not retry["archive_complete"]
    assert (
        retry["sources"][0]["object_failures"]
        == result["sources"][0]["object_failures"]
    )
    stub.assert_no_pending_responses()


def event(event_id, timestamp):
    return {
        "eventId": event_id,
        "timestamp": timestamp,
        "ingestionTime": timestamp + 1,
        "logStreamName": "stream",
        "message": "원본\n본문",
    }


def test_cloudwatch_empty_page_replay_and_late_arrival(config, aws, monkeypatch):
    cfg, _ = config
    source = cfg["sources"][1]
    cfg["sources"] = [source]
    logs, stub = aws("logs")
    first_cutoff, next_cutoff = 30 * DAY_MS, 31 * DAY_MS
    clock = iter((first_cutoff, next_cutoff, next_cutoff))
    monkeypatch.setattr(runner, "now_ms", lambda: next(clock))
    monkeypatch.setattr(
        runner, "open_source", lambda source: CloudWatch(logs, source, "aws")
    )
    query = {
        "logGroupName": "/example",
        "startTime": 0,
        "endTime": first_cutoff,
        "limit": 500,
    }
    older = event("original", 25 * DAY_MS)
    late = event("late", 24 * DAY_MS)
    stub.add_response("filter_log_events", {"events": [], "nextToken": "more"}, query)
    stub.add_response(
        "filter_log_events", {"events": [older]}, {**query, "nextToken": "more"}
    )
    stub.add_response(
        "filter_log_events",
        {"events": [late, older]},
        {
            **query,
            "startTime": 16 * DAY_MS,
            "endTime": next_cutoff,
        },
    )
    stub.add_response(
        "filter_log_events",
        {"events": [late, older]},
        {**query, "endTime": next_cutoff},
    )
    assert runner.run(cfg, "run")["sources"][0]["archived_items"] == 1
    assert runner.run(cfg, "run")["sources"][0]["archived_items"] == 2
    assert runner.run(cfg, "run", full_rescan=True)["sources"][0]["archived_items"] == 2
    archive = Archive(cfg)
    with archive.connect():
        restored = []
        for _, record in archive.records("logs"):
            assert record["archive_fidelity"] == "api_records"
            restored.extend(
                json.loads(line)
                for line in archive.archive_path(record).read_text().splitlines()
            )
        assert sorted(restored, key=lambda row: row["eventId"]) == [late, older]
    stub.assert_no_pending_responses()


def test_cloudwatch_resumes_same_cutoff_after_failed_page(config, aws, monkeypatch):
    cfg, _ = config
    source = cfg["sources"][1]
    cfg["sources"] = [source]
    logs, stub = aws("logs")
    cutoffs = iter([100, 200])
    monkeypatch.setattr(runner, "now_ms", lambda: next(cutoffs))
    monkeypatch.setattr(
        runner, "open_source", lambda source: CloudWatch(logs, source, "aws")
    )
    query = {"logGroupName": "/example", "startTime": 0, "endTime": 100, "limit": 500}
    first = event("original", 50)
    stub.add_response(
        "filter_log_events", {"events": [first], "nextToken": "next"}, query
    )
    stub.add_client_error(
        "filter_log_events",
        service_error_code="InvalidParameterException",
        expected_params={**query, "nextToken": "next"},
    )
    stub.add_response(
        "filter_log_events", {"events": [first, event("other", 75)]}, query
    )
    assert runner.run(cfg, "run")["sources"][0]["active_through_ms"] == 100
    resumed = runner.run(cfg, "run")
    assert resumed["archive_complete"]
    assert resumed["sources"][0]["completed_through_ms"] == 100
    assert resumed["sources"][0]["archived_items"] == 2
    stub.assert_no_pending_responses()


@pytest.mark.parametrize("after_publication", [False, True])
def test_resume_after_interrupted_commit(config, monkeypatch, after_publication):
    cfg, _ = config
    archive = Archive(cfg)
    payload = b"archive bytes"

    def fail(*args, **kwargs):
        raise OSError("simulated interruption")

    args = ("trail", ["stable-id"])
    kwargs = dict(
        object_key="trail/a",
        chunks=[payload],
        size=len(payload),
        fidelity="source_file",
    )
    with locked(cfg, write=True), archive.connect(write=True):
        with monkeypatch.context() as patch:
            if after_publication:
                patch.setattr(archive, "publish_manifest", fail)
            else:
                patch.setattr(os, "link", fail)
            with pytest.raises(OSError):
                archive.write(*args, **kwargs)
        assert archive.summary("trail")["pending_files"] == 1
        assert runner.run(cfg, "status")["sources"][0]["pending_files"] == 1
    with Archive(cfg).connect(write=True):
        pass
    archive = Archive(cfg)
    with archive.connect(write=True):
        assert archive.summary("trail")["pending_files"] == 0
        if not after_publication:
            assert not archive.seen("trail", ["stable-id"])
            archive.write(*args, **kwargs)
        [(key, record)] = archive.records("trail")
        assert archive.verify_file(key, record)


def test_budget_stops_before_archive_write_and_locks_destinations(config):
    cfg, _ = config
    cfg["budget_bytes"] = 8000
    with locked(cfg, write=True):
        with (
            pytest.raises(StorageBlocked, match="TRANSFER_IN_PROGRESS"),
            locked(cfg, write=True),
        ):
            pass
        archive = Archive(cfg)
        with archive.connect(write=True):
            with pytest.raises(StorageBlocked, match="CAPACITY_EXCEEDED"):
                archive.write(
                    "trail",
                    ["id"],
                    object_key="key",
                    chunks=[b"x"],
                    size=1,
                    fidelity="source_file",
                )
            assert not archive.stage.exists()
            assert archive.summary("trail")["archived_files"] == 0
    cfg["sources"][0]["prefix"] = "changed/"
    with (
        pytest.raises(StorageBlocked, match="SCOPE_OR_DESTINATION_CHANGED"),
        Archive(cfg).connect(),
    ):
        pass


def search_hit(count, *, failed=0):
    return {
        "status": 200,
        "timed_out": False,
        "_shards": {"total": 1, "failed": failed},
        "hits": {"total": {"relation": "eq", "value": count}},
    }


def test_verify_reports_wazuh_match_missing_duplicate_and_unknown(config, monkeypatch):
    cfg, _ = config
    source = cfg["sources"][0]
    cfg["sources"] = [source]
    match = {"format": "cloudtrail", "field": "data.aws.eventID", "sample_id": "known"}
    source["match"] = match
    cfg["search"] = {}
    archive = Archive(cfg)
    entries = [
        {"eventID": key} for key in ("matched", "missing", "duplicate", "partial")
    ] + [{}]
    payload = gzip.compress(json.dumps({"Records": entries}).encode())
    with archive.connect(write=True):
        archive.bounds("trail", 10, 0, False)
        archive.write(
            "trail",
            ["id"],
            object_key="key",
            chunks=[payload],
            size=len(payload),
            fidelity="source_file",
        )
        archive.finish("trail")
    search = Search.__new__(Search)

    def request(method, path, body=None):
        assert path.startswith("/wazuh-archives-*/")
        if method == "GET":
            return {"fields": {match["field"]: {"keyword": {"searchable": True}}}}
        queries = [json.loads(line) for line in body.splitlines()][1::2]
        results = {
            "known": search_hit(1),
            "matched": search_hit(1),
            "missing": search_hit(0),
            "duplicate": search_hit(2),
            "partial": search_hit(0, failed=1),
        }
        return {
            "responses": [results[q["query"]["term"][match["field"]]] for q in queries]
        }

    monkeypatch.setattr(search, "request", request)
    monkeypatch.setattr(runner, "Search", lambda _: search)
    result = runner.run(cfg, "verify")
    row = result["sources"][0]
    assert result["archive_complete"] and not result["full_migration_verified"]
    assert (
        row["matched_events"],
        row["missing_events"],
        row["duplicate_events"],
        row["unknown_events"],
    ) == (1, 1, 1, 2)

    with archive.connect():
        ((_, record),) = archive.records("trail")
    archive.archive_path(record).write_bytes(b"tampered")
    result = runner.run(cfg, "verify")
    assert not result["archive_complete"]
    assert result["sources"][0]["local_files_failed"] == 1


def test_cli_error_is_single_secret_free_result(config):
    _, path = config
    path.write_text("password: secret-value-not-for-output\n")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "migration.security_logs",
            "plan",
            "--config",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 3 and not proc.stderr
    assert "secret-value" not in proc.stdout
    assert json.loads(proc.stdout)["reason_codes"] == ["INPUT_INVALID_OR_UNREADABLE"]
