from datetime import UTC, datetime
from typing import Any

from oneport_aws.providers.oneport_ecs.gateways.base import AwsGateway, Payload


def metric_query(
    query_id: str,
    *,
    namespace: str,
    metric: str,
    dimensions: dict[str, str],
    period: int,
    stat: str,
    return_data: bool = True,
) -> Payload:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {
                "Namespace": namespace,
                "MetricName": metric,
                "Dimensions": [{"Name": k, "Value": v} for k, v in dimensions.items()],
            },
            "Period": period,
            "Stat": stat,
        },
        "ReturnData": return_data,
    }


class CloudWatchGateway(AwsGateway):
    def put_alarm(self, name: str, metrics: list[Payload]) -> None:
        self._write(
            "put_metric_alarm",
            AlarmName=name,
            Metrics=metrics,
            ComparisonOperator="GreaterThanOrEqualToThreshold",
            Threshold=1,
            EvaluationPeriods=2,
            DatapointsToAlarm=2,
            TreatMissingData="notBreaching",
            ActionsEnabled=False,
        )

    def delete_alarm(self, name: str) -> None:
        self._write("delete_alarms", AlarmNames=[name])

    def get_metric_values(
        self, queries: list[Payload], start: datetime, end: datetime
    ) -> dict[str, dict[datetime, float]]:
        values: dict[str, dict[datetime, float]] = {}
        kwargs: dict[str, Any] = {
            "MetricDataQueries": queries,
            "StartTime": start,
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        }
        while True:
            page = self._read("get_metric_data", **kwargs)
            for result in page.get("MetricDataResults", []):
                bucket = values.setdefault(result["Id"], {})
                for at, value in zip(
                    result.get("Timestamps", []), result.get("Values", []), strict=False
                ):
                    bucket[at.astimezone(UTC)] = value
            if not page.get("NextToken"):
                return values
            kwargs["NextToken"] = page["NextToken"]
