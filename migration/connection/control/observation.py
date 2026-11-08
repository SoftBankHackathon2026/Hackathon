"""Schedule observations from the authenticated agent poll and store the last result."""

import secrets

from migration_connection.contracts.protocol import signed_bytes
from migration_connection.control.errors import ConnectionError
from migration_connection.inventory.observation import (
    MAX_NAMESPACES,
    NAMESPACE_NAME,
    OBSERVE,
    OBSERVE_FRESH_MS,
    OBSERVE_INTERVAL_MS,
    validate_observation,
)


def registration_scope(row):
    report = row.get("inventory_report") or {}
    cluster = report.get("cluster", {})
    uid = cluster.get("namespace_uid")
    if not uid or report.get("target_id") != row.get("selected_id"):
        return None
    namespaces = sorted(
        {
            item["namespace"]
            for item in (report.get("workloads") or {}).get("items", [])
            if NAMESPACE_NAME.fullmatch(item["namespace"])
        }
    )
    return {
        "target_id": report["target_id"],
        "namespace_uid": uid,
        "namespaces": namespaces[:MAX_NAMESPACES],
        "scope_limited": len(namespaces) > MAX_NAMESPACES,
    }


def schedule(row, now, sign):
    if (
        not row.get("environment_id")
        or row.get("provider", "kubernetes") != "kubernetes"
    ):
        return None
    scope = row.get("observation_scope")
    if scope is None:
        scope = registration_scope(row)
        if scope is None:
            return None
        row["observation_scope"] = scope

    task = row.get("observation_task")
    if task and task["payload"]["epoch"] == row["epoch"]:
        if now < task["payload"]["expires_at_ms"]:
            return task
        row["observation_failed"] = True

    attempt = row.get("observation_attempt")
    if (
        attempt
        and attempt["epoch"] == row["epoch"]
        and now < attempt["at"] + OBSERVE_INTERVAL_MS
    ):
        return None

    payload = {
        "job_id": secrets.token_urlsafe(16),
        "operation": OBSERVE,
        "device_id": row["device_id"],
        "request_id": row["id"],
        "epoch": row["epoch"],
        "target_id": scope["target_id"],
        "namespace_uid": scope["namespace_uid"],
        "namespaces": scope["namespaces"],
        "expires_at_ms": now + 120_000,
    }
    task = {"payload": payload, "signature": sign(signed_bytes("job", payload))}
    row["observation_task"] = task
    row["observation_attempt"] = {"at": now, "epoch": row["epoch"]}
    return task


def receive(row, now, data):
    if set(data) != {"job_id", "report"}:
        raise ConnectionError("INVALID_OBSERVATION", 422)
    task = row.get("observation_task")
    payload = task["payload"] if task else {}
    if (
        not row.get("environment_id")
        or not task
        or data["job_id"] != payload.get("job_id")
        or payload.get("epoch") != row["epoch"]
        or now >= payload.get("expires_at_ms", 0)
    ):
        raise ConnectionError("STALE_OBSERVATION", 409)

    report = data["report"]
    if report is not None:
        try:
            validate_observation(
                report, payload["namespaces"], payload["namespace_uid"]
            )
        except (ValueError, TypeError, KeyError):
            raise ConnectionError("INVALID_OBSERVATION", 422) from None
        row["observation"] = {"at": now, "epoch": row["epoch"], "report": report}
    row["observation_failed"] = report is None
    row.pop("observation_task", None)
    return {"accepted": True}


def fleet_observation(row, latest, tunnel, now):
    if not latest and not row.get("observation_failed"):
        return None
    fresh = bool(
        latest
        and tunnel == "READY"
        and latest["epoch"] == row["epoch"]
        and now - latest["at"] < OBSERVE_FRESH_MS
        and not row.get("observation_failed")
    )
    return {
        "latest": latest,
        "fresh": fresh,
        "failed": bool(row.get("observation_failed")),
        "scopeLimited": row["observation_scope"]["scope_limited"],
    }
