"""Conservative capacity arithmetic; shared devices are charged only once."""

from __future__ import annotations

import math


def calculate(
    filesystems, storage, observed_source_bytes=None, coverage_complete=False
):
    devices = {}
    allocations = {
        "raw": storage["raw_budget_bytes"],
        "index": storage["index_budget_bytes"],
        "intermediate": storage["intermediate_budget_bytes"],
    }
    for name, fs in filesystems.items():
        d = devices.setdefault(
            str(fs["device"]),
            {
                "available_bytes": fs["available_bytes"],
                "allocated_bytes": 0,
                "paths": [],
            },
        )
        d["available_bytes"] = min(d["available_bytes"], fs["available_bytes"])
        used = fs.get("managed_used_bytes", 0)
        d["allocated_bytes"] += max(0, allocations[name] - used)
        d["paths"].append(fs["path"])
    for d in devices.values():
        d["reserve_bytes"] = storage["reserve_bytes"]
        d["remaining_bytes"] = (
            d["available_bytes"] - d["reserve_bytes"] - d["allocated_bytes"]
        )
    fits = all(d["remaining_bytes"] >= 0 for d in devices.values())
    source = storage.get("source_raw_bytes")
    basis = "platform_input" if source is not None else "unknown"
    if source is None and coverage_complete:
        source, basis = observed_source_bytes, "complete_source_listing"
    decoded = storage.get("decoded_bytes")
    low, high = storage.get("index_factor_low"), storage.get("index_factor_high")
    needed = None
    if (
        source is not None
        and decoded is not None
        and low is not None
        and high is not None
    ):
        needed = {
            "raw_bytes": source,
            "index_low_bytes": math.ceil(decoded * low),
            "index_high_bytes": math.ceil(decoded * high),
        }
    all_logs_fit = (
        None
        if needed is None
        else source <= allocations["raw"]
        and needed["index_high_bytes"] <= allocations["index"]
        and fits
    )
    daily = storage.get("daily_raw_bytes")
    return {
        "devices": devices,
        "budget_fits": fits,
        "raw_budget_bytes": allocations["raw"],
        "index_budget_bytes": allocations["index"],
        "intermediate_budget_bytes": allocations["intermediate"],
        "source_raw_bytes": source,
        "source_size_basis": basis,
        "listing_complete": coverage_complete,
        "estimated_need": needed,
        "all_selected_logs_fit": all_logs_fit,
        "estimated_raw_retention_days": allocations["raw"] / daily if daily else None,
        "estimated_search_retention_days": None,
        "reason_codes": ([] if fits else ["STORAGE_BUDGET_EXCEEDS_AVAILABLE"])
        + ([] if needed else ["INDEX_SIZE_ESTIMATE_UNAVAILABLE"]),
        "budget_is_reservation": False,
    }
