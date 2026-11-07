"""Plan, transfer, inspect progress and verify the selected archive scope."""

import errno
import sqlite3
from collections import Counter
from contextlib import nullcontext

from ..wazuh_infra.config import ConfigError
from .aws import DAY_MS, aws_reason, open_source
from .errors import MigrationError, StorageBlocked
from .search import Search, reconcile
from .storage import Archive, assert_writer, locked, now_ms


def cloud_errors():
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise MigrationError("AWS_DEPENDENCY_UNAVAILABLE") from None
    return BotoCoreError, ClientError


def plan(config, archive, cutoff):
    errors = cloud_errors()
    capacity = archive.capacity()
    output = []
    for source in config["sources"]:
        row = {"id": source["id"], "type": source["type"], "reason_codes": []}
        try:
            provider = open_source(source)
            row.update(
                provider.plan(
                    config["plan_pages"] if source["type"] == "s3" else cutoff
                )
            )
        except errors as error:
            row["reason_codes"].append(aws_reason(error))
        except MigrationError as error:
            row["reason_codes"].append(str(error))
        output.append(row)
    all_s3_listed = all(
        row["type"] == "s3" and row.get("listing_complete") is True for row in output
    )
    capacity["initial_copy_bytes_if_all_sources_listed"] = (
        sum(row["bytes_observed"] for row in output) if all_s3_listed else None
    )
    capacity["fits_initial_copy"] = (
        capacity["initial_copy_bytes_if_all_sources_listed"]
        <= capacity["raw_writable_bytes"]
        if all_s3_listed
        else None
    )
    return output, capacity


def transfer(config, archive, cutoff, full_rescan):
    errors = cloud_errors()
    output = []
    stop_reason = None
    for source in config["sources"]:
        source_id = source["id"]
        row = {"id": source_id, "type": source["type"], "reason_codes": []}
        if stop_reason:
            row["reason_codes"].append("SOURCE_SKIPPED_AFTER_DESTINATION_BLOCK")
        else:
            try:
                start, end = archive.bounds(
                    source_id,
                    cutoff,
                    source.get("lookback_days", 1) * DAY_MS,
                    full_rescan,
                )
                provider = open_source(source)
                if source["type"] == "s3":
                    row.update(provider.archive(archive, end))
                else:
                    row.update(provider.archive(archive, start, end))
                if not row["reason_codes"]:
                    archive.finish(source_id)
            except StorageBlocked as error:
                stop_reason = str(error)
                row["reason_codes"].append(stop_reason)
            except errors as error:
                row["reason_codes"].append(aws_reason(error))
            except MigrationError as error:
                row["reason_codes"].append(str(error))
        row.update(archive.summary(source_id))
        output.append(row)
    return output


def status_source(archive, source):
    output = {
        "id": source["id"],
        "type": source["type"],
        **archive.summary(source["id"]),
    }
    if source["type"] == "s3":
        output["object_failures"] = archive.s3_issues(source["id"])
    return output


def verify_source(archive, source, search, search_reason):
    row = archive.summary(source["id"])
    row.update(id=source["id"], type=source["type"])
    match = source.get("match")
    reasons = set()
    if not match:
        reasons.add("WAZUH_MAPPING_NOT_CONFIGURED")
    elif search is None:
        reasons.add(search_reason)
    else:
        try:
            search.check_mapping(match)
        except MigrationError as error:
            reasons.add(str(error))
    can_search = not reasons

    local_verified = local_failed = unknown_files = 0
    counts = Counter(
        matched_events=0, missing_events=0, duplicate_events=0, unknown_events=0
    )
    for key, record in archive.records(source["id"]):
        try:
            intact = archive.verify_file(key, record)
        except (OSError, ValueError, MigrationError):
            intact = False
        if not intact:
            local_failed += 1
            unknown_files += 1
            continue
        local_verified += 1
        if not can_search:
            unknown_files += 1
            continue
        try:
            counts.update(
                reconcile(
                    search,
                    archive.archive_path(record),
                    record["archive_fidelity"],
                    match,
                )
            )
        except MigrationError as error:
            reasons.add(str(error))
            unknown_files += 1
        except (OSError, EOFError, ValueError):
            unknown_files += 1

    archive_complete = (
        row["completed_through_ms"] is not None
        and row["active_through_ms"] is None
        and not row["pending_files"]
        and not local_failed
    )
    search_complete = (
        archive_complete
        and can_search
        and counts["matched_events"] > 0
        and not any(
            counts[field]
            for field in ("missing_events", "duplicate_events", "unknown_events")
        )
        and not unknown_files
    )
    if local_failed:
        reasons.add("LOCAL_ARCHIVE_VERIFICATION_FAILED")
    if not archive_complete:
        reasons.add("ARCHIVE_SCOPE_INCOMPLETE")
    if not search_complete:
        reasons.add("SEARCH_RECONCILIATION_INCOMPLETE")
    row.update(
        local_files_verified=local_verified,
        local_files_failed=local_failed,
        search_unknown_files=unknown_files,
        archive_verified=archive_complete,
        search_verified=search_complete,
        reason_codes=sorted(reasons),
        **counts,
    )
    return row


def run(config, operation, full_rescan=False):
    assert_writer()
    result = {
        "schema_version": 1,
        "operation": operation,
        "status": "complete",
        "archive_complete": False,
        "search_verified": False,
        "full_migration_verified": False,
        "reason_codes": [],
        "sources": [],
    }
    try:
        access = (
            nullcontext()
            if operation == "status"
            else locked(config, write=operation == "run")
        )
        with access:
            archive = Archive(config)
            if operation == "plan":
                with archive.connect():
                    result["sources"], result["capacity"] = plan(
                        config, archive, now_ms()
                    )
            else:
                with archive.connect(write=operation == "run") as exists:
                    if not exists:
                        raise StorageBlocked("TRANSFER_STATE_NOT_FOUND")
                    if operation == "run":
                        result["sources"] = transfer(
                            config, archive, now_ms(), full_rescan
                        )
                    elif operation == "status":
                        result["sources"] = [
                            {
                                "id": s["id"],
                                "type": s["type"],
                                **archive.summary(s["id"]),
                            }
                            for s in config["sources"]
                        ]
                    else:
                        search = None
                        reason = "WAZUH_SEARCH_NOT_CONFIGURED"
                        if "search" in config:
                            try:
                                search = Search(config["search"])
                            except (ConfigError, OSError, MigrationError):
                                reason = "WAZUH_SEARCH_CREDENTIAL_OR_CA_UNAVAILABLE"
                        result["sources"] = [
                            verify_source(archive, s, search, reason)
                            for s in config["sources"]
                        ]
                result["archive_complete"] = (
                    all(
                        row.get("completed_through_ms") is not None
                        and row.get("active_through_ms") is None
                        and not row.get("pending_files")
                        and not row.get("reason_codes")
                        for row in result["sources"]
                    )
                    if operation != "verify"
                    else all(row["archive_verified"] for row in result["sources"])
                )
                if operation == "verify":
                    result["search_verified"] = all(
                        row["search_verified"] for row in result["sources"]
                    )
                    result["full_migration_verified"] = (
                        result["archive_complete"] and result["search_verified"]
                    )

            reasons = {
                reason
                for row in result["sources"]
                for reason in row.get("reason_codes", [])
            }
            if (
                operation in {"run", "status", "verify"}
                and not result["archive_complete"]
            ):
                reasons.add("ARCHIVE_SCOPE_INCOMPLETE")
            if reasons:
                result.update(status="partial", reason_codes=sorted(reasons))
    except StorageBlocked as error:
        result.update(status="blocked", reason_codes=[str(error)])
    except MigrationError as error:
        result.update(status="failed", reason_codes=[str(error)])
    except OSError as error:
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            result.update(
                status="blocked", reason_codes=["DESTINATION_CAPACITY_EXCEEDED"]
            )
        else:
            result.update(status="failed", reason_codes=["LOCAL_IO_OR_STATE_FAILED"])
    except sqlite3.Error as error:
        if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
            result.update(
                status="blocked", reason_codes=["DESTINATION_CAPACITY_EXCEEDED"]
            )
        else:
            result.update(status="failed", reason_codes=["LOCAL_IO_OR_STATE_FAILED"])
    except ValueError:
        result.update(status="failed", reason_codes=["LOCAL_IO_OR_STATE_FAILED"])
    return result
