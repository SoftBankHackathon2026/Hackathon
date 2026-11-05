import json
from datetime import datetime
from typing import Any

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload


class XrayGateway(AwsGateway):
    def list_trace_summaries(
        self, *, start: datetime, end: datetime, filter_expression: str, limit: int, max_pages: int
    ) -> list[Payload]:
        kwargs: dict[str, Any] = {
            "StartTime": start,
            "EndTime": end,
            "Sampling": False,
            "FilterExpression": filter_expression,
        }
        found: list[Payload] = []
        for _ in range(max_pages):
            page = self._read("get_trace_summaries", **kwargs)
            found += page.get("TraceSummaries", [])
            if len(found) >= limit or not page.get("NextToken"):
                break
            kwargs["NextToken"] = page["NextToken"]
        return found

    def get_trace_documents(self, trace_id: str) -> list[Payload] | None:
        page = self._read("batch_get_traces", TraceIds=[trace_id])
        traces = page.get("Traces", [])
        if not traces:
            return None
        return [json.loads(segment["Document"]) for segment in traces[0].get("Segments", [])]
