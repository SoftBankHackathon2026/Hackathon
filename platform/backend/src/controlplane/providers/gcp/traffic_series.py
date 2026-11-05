from datetime import UTC, datetime, timedelta

from controlplane.providers.gcp.trace_parser import timestamp
from controlplane.providers.telemetry import latest_resource_sample

EMPTY_POINT = {
    "requests": 0,
    "server_errors": 0,
    "client_errors": 0,
    "p95_ms": None,
    "healthy": None,
}


def floor_time(moment, period):
    return datetime.fromtimestamp(int(moment.timestamp()) // period * period, UTC)


def service_weights(service) -> dict[str, int]:
    weights: dict[str, int] = {}
    for t in service.get("trafficStatuses", []):
        revision = t.get("revision", "").rsplit("/", 1)[-1]
        if revision:
            weights[revision] = weights.get(revision, 0) + int(t.get("percent", 0))
    return weights


def collect_points(metrics, period) -> tuple[set[str], dict]:
    revisions: set[str] = set()
    points: dict = {}
    for metric, values in metrics:
        for series in values:
            revision = series.get("resource", {}).get("labels", {}).get("revision_name")
            if not revision:
                continue
            revisions.add(revision)
            code = series.get("metric", {}).get("labels", {}).get("response_code_class", "")
            for point in series.get("points", []):
                at = floor_time(
                    timestamp(point["interval"]["endTime"]) - timedelta(seconds=period), period
                )
                value = point.get("value", {})
                number = float(value.get("doubleValue", value.get("int64Value", 0)))
                bucket = points.setdefault((revision, at), {})
                bucket[metric] = bucket.get(metric, 0) + number
                if metric == "requests" and code in {"4xx", "5xx"}:
                    key = "client_errors" if code == "4xx" else "server_errors"
                    bucket[key] = bucket.get(key, 0) + number
    return revisions, points


def build_series(revisions, weights, points, grid) -> list[dict]:
    ordered = sorted(revisions, key=lambda r: (-weights.get(r, 0), r))
    return [
        {
            "color": "blue" if i == 0 else "green",
            "label": revision,
            "target_group": revision,
            "weight": weights.get(revision, 0),
            "points": [{"at": at, **EMPTY_POINT, **points.get((revision, at), {})} for at in grid],
        }
        for i, revision in enumerate(ordered)
    ]


def latest_utilization(series, env, now):
    buckets: dict = {}
    for row in series:
        labels = row.get("resource", {}).get("labels", {})
        if (
            labels.get("service_name") != env.service
            or labels.get("project_id") != env.project_id
            or labels.get("location") != env.region
        ):
            continue
        for point in row.get("points", []):
            distribution = point.get("value", {}).get("distributionValue", {})
            count = int(distribution.get("count", 0))
            if count <= 0 or "mean" not in distribution:
                continue
            at = timestamp(point["interval"]["endTime"])
            total, samples = buckets.get(at, (0.0, 0))
            buckets[at] = (total + float(distribution["mean"]) * count, samples + count)
    return latest_resource_sample(
        {at: total / count * 100 for at, (total, count) in buckets.items()}, now
    )


def instance_counts(instances) -> dict:
    counts: dict = {}
    for row in instances:
        for point in row.get("points", []):
            at = timestamp(point["interval"]["endTime"])
            value = point.get("value", {})
            count = float(value.get("int64Value", value.get("doubleValue", 0)))
            counts[at] = counts.get(at, 0) + count
    return counts
