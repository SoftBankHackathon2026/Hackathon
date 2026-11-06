from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from controlplane.domain.fleet import (
    ClusterFacts,
    ContainerFact,
    EndpointFact,
    InventoryItem,
    NodeFact,
    PodFact,
    Resources,
    WorkerSample,
    Workload,
)
from controlplane.domain.migration import Stage
from controlplane.schemas.migrations import (
    AppState,
    ConnectorView,
    GateDecision,
    LatencyView,
    VerifyReport,
    WorkerView,
)

Row = dict[str, Any]


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def sample_from_status(status: Row, at: datetime) -> WorkerSample:
    latency = status.get("latency_seconds") or {}
    return WorkerSample(
        at=at,
        state=str(status.get("state", "UNKNOWN")),
        target=str(status.get("target", "")),
        phase=str(status.get("phase", "")),
        queue_length=int(status.get("queue_length") or 0),
        committed_by_target={
            str(k): int(v) for k, v in (status.get("committed_by_target") or {}).items()
        },
        counters={str(k): int(v) for k, v in (status.get("counters") or {}).items()},
        latency_p95=latency.get("p95"),
        latency_max=latency.get("max"),
        rate_per_second=float(status.get("rate_per_second") or 0),
        latency_p50=latency.get("p50"),
    )


def sample_to_json(sample: WorkerSample) -> Row:
    payload = asdict(sample)
    payload.pop("at")
    return payload


def sample_from_json(at: datetime, payload: Row) -> WorkerSample:
    return WorkerSample(at=at, **payload)


def worker_view(sample: WorkerSample) -> WorkerView:
    return WorkerView(
        state=sample.state,
        target=sample.target,
        phase=sample.phase,
        queue_length=sample.queue_length,
        rate_per_second=sample.rate_per_second,
        latency=LatencyView(p50=sample.latency_p50, p95=sample.latency_p95, max=sample.latency_max),
        committed_by_target=sample.committed_by_target,
    )


def connector_view(name: str, status: Row) -> ConnectorView:
    tasks = status.get("tasks") or []
    return ConnectorView(
        name=name,
        role="source" if name.endswith("-source") else "sink",
        state=str((status.get("connector") or {}).get("state", "UNKNOWN")),
        tasks_running=sum(1 for task in tasks if task.get("state") == "RUNNING"),
        tasks_total=len(tasks),
    )


def gate_from_decision(record: Row) -> GateDecision:
    return GateDecision(
        decision_id=str(record["decision_id"]),
        stage=record["stage"],
        outcome=record["outcome"],
        reason_codes=list(record.get("reason_codes") or []),
        policy_version=record.get("policy_version"),
        evaluated_at=parse_time(record["evaluated_at"]),
    )


def verify_from_report(report: Row) -> VerifyReport:
    return VerifyReport.model_validate(
        {
            key: report[key]
            for key in ("passed", "counts", "source_count", "target_count", "committed_count")
        }
    )


def cutover_from_status(status: Row) -> tuple[Stage, Row, datetime] | None:
    stage = status.get("stage")
    if not stage:
        return None
    return Stage(stage), dict(status.get("checkpoint") or {}), parse_time(status["updated_at"])


def _pod_to_json(pod: PodFact) -> Row:
    payload = asdict(pod)
    payload["started_at"] = pod.started_at.isoformat() if pod.started_at else None
    return payload


def _pod_from_json(payload: Row) -> PodFact:
    started = payload.get("started_at")
    return PodFact(
        **{
            **payload,
            "containers": [ContainerFact(**c) for c in payload.get("containers") or []],
            "started_at": parse_time(started) if started else None,
        }
    )


def _item_to_json(item: InventoryItem) -> Row:
    payload = asdict(item)
    payload["pods"] = None if item.pods is None else [_pod_to_json(pod) for pod in item.pods]
    return payload


def _item_from_json(payload: Row) -> InventoryItem:
    pods = payload.get("pods")
    return InventoryItem(
        **{
            **payload,
            "pods": None if pods is None else [_pod_from_json(pod) for pod in pods],
            "endpoints": [EndpointFact(**e) for e in payload.get("endpoints") or []],
        }
    )


def _resources(payload: Row | None) -> Resources | None:
    return None if payload is None else Resources(**payload)


def _node_from_json(payload: Row) -> NodeFact:
    sized = {key: _resources(payload.get(key)) for key in ("allocatable", "requests", "usage")}
    return NodeFact(**{**payload, **sized})


def facts_to_json(facts: ClusterFacts) -> Row:
    payload = asdict(facts)
    payload["observed_at"] = facts.observed_at.isoformat() if facts.observed_at else None
    payload["default_deny_namespaces"] = sorted(facts.default_deny_namespaces)
    payload["resource_quota_namespaces"] = sorted(facts.resource_quota_namespaces)
    payload["apps"] = [app.model_dump() for app in facts.apps]
    if facts.inventory is not None:
        payload["inventory"] = [_item_to_json(item) for item in facts.inventory]
    return payload


def facts_from_json(payload: Row) -> ClusterFacts:
    return ClusterFacts(
        observed_at=parse_time(payload["observed_at"]) if payload.get("observed_at") else None,
        connected=bool(payload.get("connected")),
        namespace_labels=payload.get("namespace_labels") or {},
        default_deny_namespaces=set(payload.get("default_deny_namespaces") or []),
        network_policy_count=payload.get("network_policy_count") or {},
        workloads=[Workload(**item) for item in payload.get("workloads") or []],
        wildcard_bindings=payload.get("wildcard_bindings"),
        argocd_admin_enabled=payload.get("argocd_admin_enabled"),
        argocd_anonymous_enabled=payload.get("argocd_anonymous_enabled"),
        external_secrets=payload.get("external_secrets"),
        resource_quota_namespaces=set(payload.get("resource_quota_namespaces") or []),
        apps=[AppState.model_validate(item) for item in payload.get("apps") or []],
        workload_ready={
            k: (int(v[0]), int(v[1])) for k, v in (payload.get("workload_ready") or {}).items()
        },
        read_status=payload.get("read_status"),
        inventory=None
        if payload.get("inventory") is None
        else [_item_from_json(item) for item in payload["inventory"]],
        version=payload.get("version"),
        nodes=None
        if payload.get("nodes") is None
        else [_node_from_json(node) for node in payload["nodes"]],
    )
