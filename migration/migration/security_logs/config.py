"""Validate the Wazuh handoff and the explicitly selected AWS sources."""

import json
import re
from pathlib import Path

import yaml

from ..wazuh_infra.config import (
    absolute,
    integer,
    keys,
    require,
    secret,
    text,
)

ACCOUNT = re.compile(r"[0-9]{12}")
REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+")
FIELD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,255}")


def load(path):
    config = yaml.safe_load(secret(path))
    keys(
        config,
        "schema_version wazuh_result reserve_bytes sources search plan_pages",
        "config",
    )
    require(
        type(config.get("schema_version")) is int and config["schema_version"] == 1,
        "schema_version",
    )
    result = json.loads(secret(absolute(config.get("wazuh_result"), "wazuh_result")))
    require(
        isinstance(result, dict)
        and result.get("schema_version") == 1
        and result.get("operation") == "verify"
        and result.get("status") in {"complete", "partial"},
        "wazuh_result",
    )
    reports = result.get("reports")
    require(isinstance(reports, list), "wazuh_result.reports")
    contracts = [
        report["raw_archive_contract"]
        for report in reports
        if isinstance(report, dict)
        and report.get("readiness", {}).get("raw_storage_ready") is True
        and "raw_archive_contract" in report
    ]
    require(len(contracts) == 1, "wazuh_result.raw_archive_contract")
    contract = contracts[0]
    require(
        isinstance(contract, dict)
        and contract.get("schema_version") == 1
        and contract.get("owner") == "oneport-archive",
        "archive_contract",
    )
    root = Path(absolute(contract.get("root"), "archive_contract.root"))
    manifests = Path(
        absolute(contract.get("manifest_path"), "archive_contract.manifest_path")
    )
    require(
        not root.is_relative_to(manifests) and not manifests.is_relative_to(root),
        "archive_contract.paths",
    )
    config.update(
        root=root,
        manifests=manifests,
        budget_bytes=integer(
            contract.get("budget_bytes"), "archive_contract.budget_bytes", 1
        ),
        reserve_bytes=integer(config.get("reserve_bytes"), "reserve_bytes", 1),
        plan_pages=integer(config.get("plan_pages", 10), "plan_pages", 1, 1000),
    )

    require(isinstance(config.get("sources"), list) and config["sources"], "sources")
    ids, locations = set(), []
    for source in config["sources"]:
        validate_source(source)
        require(source["id"] not in ids, "sources.id")
        ids.add(source["id"])
        if source["type"] == "s3":
            location = ("s3", source["bucket"], source["prefix"])
            require(
                not any(
                    kind == "s3"
                    and bucket == location[1]
                    and (
                        prefix.startswith(location[2]) or location[2].startswith(prefix)
                    )
                    for kind, bucket, prefix in locations
                ),
                "sources.overlapping_prefix",
            )
        else:
            location = (
                "cloudwatch",
                (source["account_id"], source["region"]),
                source["log_group"],
            )
            require(location not in locations, "sources.duplicate_log_group")
        locations.append(location)

    if "search" in config:
        search = config["search"]
        keys(search, "ca_file password_file", "search")
        for field in ("ca_file", "password_file"):
            absolute(search.get(field), "search." + field)
    require(
        "search" in config or not any("match" in s for s in config["sources"]),
        "search",
    )
    return config


def validate_source(source):
    keys(
        source,
        "id type region profile account_id credential_account_id bucket bucket_owner prefix log_group lookback_days match",
        "sources",
    )
    text(source.get("id"), "sources.id")
    require(source.get("type") in ("s3", "cloudwatch"), "sources.type")
    text(source.get("region"), "sources.region", REGION)
    text(source.get("account_id"), "sources.account_id", ACCOUNT)
    text(
        source.setdefault("credential_account_id", source["account_id"]),
        "sources.credential_account_id",
        ACCOUNT,
    )
    if "profile" in source:
        text(source["profile"], "sources.profile")
    if source["type"] == "s3":
        require(not {"log_group", "lookback_days"} & source.keys(), "sources.s3")
        text(
            source.get("bucket"),
            "sources.bucket",
            re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"),
        )
        text(
            source.setdefault("bucket_owner", source["account_id"]),
            "sources.bucket_owner",
            ACCOUNT,
        )
        prefix = source.setdefault("prefix", "")
        require(
            isinstance(prefix, str) and len(prefix.encode()) <= 1024, "sources.prefix"
        )
    else:
        require(
            not {"bucket", "bucket_owner", "prefix"} & source.keys(),
            "sources.cloudwatch",
        )
        text(
            source.get("log_group"),
            "sources.log_group",
            re.compile(r"[A-Za-z0-9_./#-]{1,512}"),
        )
        source["lookback_days"] = integer(
            source.get("lookback_days", 14), "sources.lookback_days", 1, 3650
        )
    if "match" in source:
        match = source["match"]
        keys(match, "format field sample_id", "sources.match")
        require(
            match.get("format")
            in (
                ("cloudtrail",)
                if source["type"] == "s3"
                else ("cloudtrail", "cloudwatch")
            ),
            "sources.match.format",
        )
        text(match.get("field"), "sources.match.field", FIELD)
        require(
            isinstance(match.get("sample_id"), str)
            and 1 <= len(match["sample_id"]) <= 512,
            "sources.match.sample_id",
        )


def scope(source):
    """Identity fields stay fixed after the first scan; credentials can be renewed."""
    return {
        name: source[name]
        for name in (
            "type",
            "account_id",
            "region",
            "bucket",
            "bucket_owner",
            "prefix",
            "log_group",
        )
        if name in source
    }
