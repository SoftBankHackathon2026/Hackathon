"""Read-only AWS operations and provider-specific replay identities."""

import base64
import binascii
from contextlib import closing
from datetime import UTC, datetime
from itertools import islice

from .errors import MigrationError, StorageBlocked
from .storage import CHUNK, canonical, identity

DAY_MS = 86400000


def aws_reason(error):
    response = getattr(error, "response", {})
    code = response.get("Error", {}).get("Code", "")
    if code == "InvalidObjectState":
        return "SOURCE_RESTORE_REQUIRED"
    if code in {"PreconditionFailed", "412", "NoSuchKey", "NoSuchVersion", "404"}:
        return "SOURCE_OBJECT_CHANGED"
    if code in {
        "AccessDenied",
        "AccessDeniedException",
        "403",
        "UnauthorizedOperation",
    }:
        return "SOURCE_ACCESS_DENIED"
    if code in {"ExpiredToken", "ExpiredTokenException", "InvalidClientTokenId"}:
        return "SOURCE_CREDENTIALS_INVALID"
    if code == "ResourceNotFoundException":
        return "SOURCE_NOT_FOUND"
    return "AWS_REQUEST_FAILED"


def open_source(source):
    import boto3
    from botocore.config import Config

    session = boto3.Session(
        profile_name=source.get("profile"), region_name=source["region"]
    )
    options = Config(
        retries={"mode": "standard", "total_max_attempts": 5},
        connect_timeout=10,
        read_timeout=60,
        ignore_configured_endpoint_urls=True,
    )
    caller = session.client("sts", config=options).get_caller_identity()
    if caller["Account"] != source["credential_account_id"]:
        raise MigrationError("SOURCE_CREDENTIAL_ACCOUNT_MISMATCH")
    if source["type"] == "s3":
        return S3(session.client("s3", config=options), source)
    return CloudWatch(
        session.client("logs", config=options), source, caller["Arn"].split(":")[1]
    )


class S3:
    def __init__(self, client, source):
        self.client = client
        self.source = source
        self.bucket = {
            "Bucket": source["bucket"],
            "ExpectedBucketOwner": source["bucket_owner"],
        }

    def pages(self):
        return self.client.get_paginator("list_objects_v2").paginate(
            **self.bucket, Prefix=self.source["prefix"]
        )

    def get(self, obj, **params):
        return self.client.get_object(
            **self.bucket, Key=obj["Key"], IfMatch=obj["ETag"], **params
        )

    def plan(self, limit):
        from botocore.exceptions import BotoCoreError, ClientError

        total, count, first = 0, 0, None
        last = {"IsTruncated": True}
        for last in islice(self.pages(), limit):
            for obj in last.get("Contents", []):
                first = first if first is not None else obj
                total += obj["Size"]
                count += 1
        result = {
            "objects_observed": count,
            "bytes_observed": total,
            "listing_complete": not last["IsTruncated"],
            "sample_readable": None,
        }
        if first is not None:
            params = {"Range": "bytes=0-0"} if first["Size"] else {}
            try:
                with closing(self.get(first, **params)["Body"]) as body:
                    body.read(1)
                result["sample_readable"] = True
            except (BotoCoreError, ClientError) as error:
                result.update(sample_readable=False, reason_codes=[aws_reason(error)])
        return result

    def archive(self, archive, cutoff):
        from botocore.exceptions import BotoCoreError, ClientError

        source_id = self.source["id"]
        boundary = datetime.fromtimestamp(cutoff / 1000, UTC)
        deferred = 0
        previous_issues = archive.s3_issues(source_id)
        for page in self.pages():
            for obj in page.get("Contents", []):
                if obj["LastModified"] > boundary:
                    deferred += 1
                    continue
                item = identity(
                    [
                        obj["Key"],
                        obj["ETag"],
                        obj["Size"],
                        obj["LastModified"].isoformat(),
                    ]
                )
                if archive.seen(source_id, [item]):
                    if previous_issues:
                        archive.clear_s3_issue(source_id, item)
                    continue
                try:
                    archive.check_space(obj["Size"])
                    self.copy(archive, obj, item)
                except (BotoCoreError, ClientError) as error:
                    archive.s3_issue(source_id, item, obj["Key"], aws_reason(error))
                except StorageBlocked:
                    archive.s3_issue(
                        source_id, item, obj["Key"], "SOURCE_COPY_INTERRUPTED"
                    )
                    raise
                except MigrationError as error:
                    if str(error) not in {
                        "SOURCE_OBJECT_CHANGED",
                        "SOURCE_LENGTH_MISMATCH",
                        "SOURCE_CHECKSUM_MISMATCH",
                    }:
                        raise
                    archive.s3_issue(source_id, item, obj["Key"], str(error))
                else:
                    if previous_issues:
                        archive.clear_s3_issue(source_id, item)
        reasons = archive.s3_issues(source_id)
        return {
            "deferred_objects": deferred,
            "object_failures": reasons,
            "reason_codes": sorted(reasons),
        }

    def copy(self, archive, obj, item):
        from botocore.exceptions import ClientError

        try:
            response = self.get(obj, ChecksumMode="ENABLED")
        except ClientError as error:
            if aws_reason(error) != "SOURCE_ACCESS_DENIED":
                raise
            # An encrypted object may be readable without the extra checksum permissions.
            response = self.get(obj)
        with closing(response["Body"]) as body:
            if (
                response.get("ETag") != obj["ETag"]
                or response.get("LastModified") != obj["LastModified"]
                or response.get("ContentLength") != obj["Size"]
            ):
                raise MigrationError("SOURCE_OBJECT_CHANGED")
            checksum = response.get("ChecksumSHA256")
            remote_sha256 = None
            if checksum is not None and response.get("ChecksumType") == "FULL_OBJECT":
                try:
                    digest = base64.b64decode(checksum, validate=True)
                    if len(digest) != 32:
                        raise ValueError
                    remote_sha256 = digest.hex()
                except (ValueError, binascii.Error):
                    raise MigrationError("SOURCE_CHECKSUM_MISMATCH") from None
            archive.write(
                self.source["id"],
                [item],
                object_key=obj["Key"],
                chunks=iter(lambda: body.read(CHUNK), b""),
                size=obj["Size"],
                fidelity="source_file",
                version=response.get("VersionId"),
                remote_sha256=remote_sha256,
            )


class CloudWatch:
    def __init__(self, client, source, partition):
        self.client = client
        self.source = source
        self.group = source["log_group"]
        self.request = {"logGroupName": self.group}
        if source["credential_account_id"] != source["account_id"]:
            self.request = {
                "logGroupIdentifier": f"arn:{partition}:logs:{source['region']}:{source['account_id']}:log-group:{self.group}"
            }

    def plan(self, cutoff):
        page = self.client.filter_log_events(
            **self.request, startTime=0, endTime=cutoff, limit=1
        )
        return {
            "events_observed": len(page.get("events", [])),
            "sample_readable": True,
            "bytes_observed": None,
            "listing_complete": not page.get("nextToken"),
        }

    def archive(self, archive, start, end):
        seen_tokens = set()
        params = {**self.request, "startTime": start, "endTime": end, "limit": 500}
        while True:
            page = self.client.filter_log_events(**params)
            self.copy_page(archive, page.get("events", []))
            token = page.get("nextToken")
            if not token:
                return {}
            if token in seen_tokens:
                raise MigrationError("SOURCE_PAGINATION_REPEATED")
            seen_tokens.add(token)
            params["nextToken"] = token

    def copy_page(self, archive, events):
        source = self.source["id"]
        unique = {}
        for event in events:
            if not isinstance(event.get("eventId"), str) or not event["eventId"]:
                raise MigrationError("CLOUDWATCH_EVENT_ID_UNAVAILABLE")
            item = identity([event["logStreamName"], event["eventId"]])
            unique.setdefault(item, event)
            if unique[item] != event:
                raise MigrationError("CLOUDWATCH_EVENT_ID_CONFLICT")
        if not unique:
            return
        existing = archive.seen(source, unique)
        ids = [item for item in unique if item not in existing]
        if not ids:
            return
        payload = "".join(canonical(unique[item]) + "\n" for item in ids).encode()
        archive.write(
            source,
            ids,
            object_key=f"{self.group}#batch-{identity(ids)}",
            chunks=[payload],
            size=len(payload),
            fidelity="api_records",
        )
