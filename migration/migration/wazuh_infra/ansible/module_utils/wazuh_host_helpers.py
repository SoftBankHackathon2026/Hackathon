"""Bounded source access checks and storage guard; no source writes."""

import configparser
import os
import subprocess

try:
    from ansible.module_utils.wazuh_common import (
        STATE,
        no_symlinks,
        read,
        storage_identity,
    )
except ImportError:
    from wazuh_common import STATE, no_symlinks, read, storage_identity


def assess_sources(c, credentials):
    results = []
    for s in c["sources"]:
        r = {
            "source_id": s["id"],
            "listing_complete": False,
            "observed_bytes": 0,
            "observed_objects": 0,
            "readable": False,
            "oldest_observed": None,
            "latest_observed": None,
        }
        try:
            import boto3
            from botocore.config import Config

            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(credentials[s["id"]])
            values = dict(parser[s["profile"]])
            session = boto3.Session(
                aws_access_key_id=values["aws_access_key_id"],
                aws_secret_access_key=values["aws_secret_access_key"],
                aws_session_token=values.get("aws_session_token"),
                region_name=s["region"],
            )
            sdk = Config(
                connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}
            )
            if s["type"] == "cloudwatchlogs":
                identity = session.client("sts", config=sdk).get_caller_identity()
                if identity.get("Account") != s["account_id"]:
                    raise ValueError("CLOUDWATCH_ACCOUNT_MISMATCH")
                session.client("logs", config=sdk).filter_log_events(
                    logGroupName=s["log_group"], limit=1
                )
                r.update(
                    readable=True,
                    status="pass",
                    reason_code=None,
                    observed_bytes=None,
                    archive_fidelity="api_records",
                )
                results.append(r)
                continue
            r["observed_time_basis"] = "object_last_modified"
            r["archive_fidelity"] = "source_file"
            client = session.client("s3", config=sdk)
            params = {
                "Bucket": s["bucket"],
                "Prefix": s["prefix"],
                "ExpectedBucketOwner": s.get("bucket_owner", s["account_id"]),
                "MaxKeys": 1000,
            }
            first = None
            for _ in range(s["max_pages"]):
                page = client.list_objects_v2(**params)
                for obj in page.get("Contents", []):
                    r["observed_bytes"] += obj["Size"]
                    r["observed_objects"] += 1
                    timestamp = obj["LastModified"].isoformat()
                    r["oldest_observed"] = min(
                        r["oldest_observed"] or timestamp, timestamp
                    )
                    r["latest_observed"] = max(
                        r["latest_observed"] or timestamp, timestamp
                    )
                    first = first or obj
                if not page.get("IsTruncated"):
                    r["listing_complete"] = True
                    break
                params["ContinuationToken"] = page["NextContinuationToken"]
            if first:
                response = client.get_object(
                    Bucket=s["bucket"],
                    Key=first["Key"],
                    ExpectedBucketOwner=s.get("bucket_owner", s["account_id"]),
                    Range="bytes=0-1023",
                )
                try:
                    response["Body"].read(1024)
                    r["readable"] = True
                finally:
                    response["Body"].close()
            r["status"] = (
                "pass" if r["readable"] and r["listing_complete"] else "unknown"
            )
            r["reason_code"] = (
                None if r["status"] == "pass" else "SOURCE_ASSESSMENT_PARTIAL_OR_EMPTY"
            )
        except ImportError:
            r.update(status="unknown", reason_code="REMOTE_BOTO3_REQUIRED")
        except Exception:
            r.update(status="unknown", reason_code="SOURCE_ACCESS_OR_LISTING_FAILED")
        results.append(r)
    return results


def storage_safe(c):
    expected = read(STATE / "installation.json").get("storage_identities", {})
    for name, path in (
        ("raw", c["storage"]["raw_path"]),
        ("index", c["storage"]["index_path"]),
        ("intermediate", "/var/ossec"),
    ):
        p = no_symlinks(path)
        if expected.get(name) and expected[name] != storage_identity(p):
            return False
        fs = os.statvfs(p)
        if (
            fs.f_bavail * fs.f_frsize < c["storage"]["reserve_bytes"]
            or fs.f_favail < 10000
        ):
            return False
        result = subprocess.run(
            ["du", "-sb", "--", str(p)], text=True, capture_output=True, timeout=30
        )
        if result.returncode != 0:
            return False
        used = int(result.stdout.split()[0])
        if used >= c["storage"][name + "_budget_bytes"]:
            return False
    return True
