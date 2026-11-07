"""Retain failed events instead of the vendor pipeline's on_failure drop action."""

import json
from pathlib import Path

from ansible.module_utils.wazuh_common import atomic

FAILURE_INDEX = "wazuh-ingest-failures"


def quarantine_actions():
    return [
        {
            "set": {
                "field": "oneport_ingest_error",
                "value": "{{ _ingest.on_failure_message }}",
            }
        },
        {
            "script": {
                "source": "def original = ctx.containsKey('message') ? ctx.message : ctx.full_log; def failure = ctx.oneport_ingest_error; for (def k : new ArrayList(ctx.keySet())) { if (!k.startsWith('_')) { ctx.remove(k); } } ctx.oneport_original_message = original; ctx.oneport_ingest_error = failure; ctx._index = 'wazuh-ingest-failures';"
            }
        },
        {
            "set": {
                "field": "oneport_failure_time",
                "value": "{{{ _ingest.timestamp }}}",
            }
        },
    ]


def harden_pipelines():
    for kind in ("archives", "alerts"):
        p = Path("/usr/share/filebeat/module/wazuh") / kind / "ingest/pipeline.json"
        pipeline = json.loads(p.read_text())
        pipeline["on_failure"] = quarantine_actions()
        atomic(p, json.dumps(pipeline), 0o644)


def simulation():
    return {
        "pipeline": {
            "processors": [{"json": {"field": "message", "add_to_root": True}}],
            "on_failure": quarantine_actions(),
        },
        "docs": [{"_source": {"message": "oneport-invalid-json-probe"}}],
    }
