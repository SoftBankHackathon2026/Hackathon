"""Optional real Wazuh indexer test, restricted to an isolated loopback endpoint."""

import json
import os
import urllib.request
from urllib.parse import urlsplit

import pytest


@pytest.mark.wazuh_live
def test_real_indexer_quarantines_invalid_json_without_losing_message(remote_modules):
    endpoint = os.environ.get("WAZUH_TEST_INDEXER_URL")
    if not endpoint:
        pytest.skip("WAZUH_TEST_INDEXER_URL requires an isolated local test indexer")
    parsed = urlsplit(endpoint)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
    from ansible.module_utils.wazuh_ingestion import FAILURE_INDEX, simulation

    request = urllib.request.Request(
        endpoint + "/_ingest/pipeline/_simulate",
        data=json.dumps(simulation()).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    doc = result["docs"][0]["doc"]
    assert doc["_index"] == FAILURE_INDEX, result
    assert doc["_source"]["oneport_original_message"] == "oneport-invalid-json-probe"
    assert doc["_source"]["oneport_ingest_error"]
    assert doc["_source"]["oneport_failure_time"]
