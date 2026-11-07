"""Read-only Wazuh reconciliation by an exact, sample-verified event identifier."""

import base64
import gzip
import http.client
import json
import ssl
from collections import Counter
from itertools import islice
from urllib.parse import urlencode

from ..wazuh_infra.config import local_file, secret
from .errors import MigrationError
from .storage import canonical, open_private

INDEX = "/wazuh-archives-*"
MAX_RECORD_BYTES = 64 * 1024 * 1024
BATCH = 100


class Search:
    def __init__(self, config):
        ca = local_file(config["ca_file"], "search.ca_file")
        password = secret(config["password_file"]).rstrip("\r\n")
        if not password or "\n" in password or "\r" in password:
            raise MigrationError("WAZUH_READER_PASSWORD_INVALID")
        token = base64.b64encode(f"oneport_reader:{password}".encode()).decode()
        self.authorization = f"Basic {token}"
        self.context = ssl.create_default_context(cafile=ca)

    def request(self, method, path, body=None):
        conn = http.client.HTTPSConnection(
            "127.0.0.1", 9200, context=self.context, timeout=20
        )
        try:
            conn.request(
                method,
                path,
                body,
                {
                    "Authorization": self.authorization,
                    "Content-Type": "application/x-ndjson",
                },
            )
            response = conn.getresponse()
            if response.status != 200:
                raise MigrationError("WAZUH_SEARCH_UNAVAILABLE")
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise MigrationError("WAZUH_SEARCH_RESPONSE_TOO_LARGE")
            return json.loads(data)
        except (OSError, ValueError, http.client.HTTPException):
            raise MigrationError("WAZUH_SEARCH_UNAVAILABLE") from None
        finally:
            conn.close()

    def check_mapping(self, match):
        field = match["field"]
        query = urlencode({"fields": field})
        response = self.request("GET", f"{INDEX}/_field_caps?{query}")
        try:
            mappings = response["fields"][field]
            exact = mappings["keyword"]
            if (
                set(mappings) != {"keyword"}
                or exact.get("searchable") is not True
                or exact.get("non_searchable_indices")
            ):
                raise ValueError
        except (KeyError, TypeError, AttributeError, ValueError):
            raise MigrationError("WAZUH_EXACT_FIELD_UNAVAILABLE") from None
        count = self.counts(field, [match["sample_id"]])[0]
        if count is None or count < 1:
            raise MigrationError("WAZUH_SAMPLE_NOT_FOUND")

    def counts(self, field, ids):
        body = "".join(
            "{}\n"
            + canonical(
                {
                    "size": 0,
                    "track_total_hits": True,
                    "query": {"term": {field: event_id}},
                }
            )
            + "\n"
            for event_id in ids
        ).encode()
        result = self.request("POST", f"{INDEX}/_msearch", body)
        if not isinstance(result, dict) or not isinstance(
            result.get("responses"), list
        ):
            raise MigrationError("WAZUH_SEARCH_RESPONSE_INVALID")
        if len(result["responses"]) != len(ids):
            raise MigrationError("WAZUH_SEARCH_RESPONSE_INVALID")
        return [complete_count(response) for response in result["responses"]]


def complete_count(response):
    try:
        if (
            response["status"] != 200
            or response["timed_out"] is not False
            or response["_shards"]["failed"] != 0
            or response["_shards"]["total"] == 0
            or response.get("terminated_early")
        ):
            return None
        total = response["hits"]["total"]
        if (
            total["relation"] == "eq"
            and type(total["value"]) is int
            and total["value"] >= 0
        ):
            return total["value"]
    except (KeyError, TypeError):
        pass
    return None


def records(path, fidelity):
    with open_private(path) as stream:
        if fidelity == "api_records":
            for line in stream:
                yield json.loads(line)
        else:
            magic = stream.read(2)
            stream.seek(0)
            if magic == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=stream) as compressed:
                    data = compressed.read(MAX_RECORD_BYTES + 1)
            else:
                data = stream.read(MAX_RECORD_BYTES + 1)
            if len(data) > MAX_RECORD_BYTES:
                raise ValueError
            payload = json.loads(data)
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("Records"), list)
                or not payload["Records"]
            ):
                raise ValueError
            yield from payload["Records"]


def event_ids(path, fidelity, match):
    for record in records(path, fidelity):
        if not isinstance(record, dict):
            yield None
            continue
        if match["format"] == "cloudtrail" and fidelity == "api_records":
            try:
                record = json.loads(record["message"])
            except (KeyError, TypeError, ValueError):
                yield None
                continue
        field = "eventId" if match["format"] == "cloudwatch" else "eventID"
        event_id = record.get(field) if isinstance(record, dict) else None
        yield (
            event_id
            if isinstance(event_id, str) and 1 <= len(event_id) <= 512
            else None
        )


def reconcile(search, path, fidelity, match):
    result = Counter()
    ids = event_ids(path, fidelity, match)
    while batch := list(islice(ids, BATCH)):
        valid = [event_id for event_id in batch if event_id is not None]
        result["unknown_events"] += len(batch) - len(valid)
        if valid:
            for count in search.counts(match["field"], valid):
                category = (
                    "unknown_events"
                    if count is None
                    else "missing_events"
                    if count == 0
                    else "matched_events"
                    if count == 1
                    else "duplicate_events"
                )
                result[category] += 1
    return result
