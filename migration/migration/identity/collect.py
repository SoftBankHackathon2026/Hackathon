"""Read-only collection with explicit incomplete coverage on adapter failure."""
from . import SCHEMA_VERSION
from .adapters.base import empty_inventory
from .models import Inventory, MigrationSpec, digest, primitive


def collect(spec: MigrationSpec, source_adapter, target_adapter) -> Inventory:
    sides = []
    for endpoint, adapter in ((spec.source, source_adapter), (spec.target, target_adapter)):
        try:
            side = adapter.collect()
        except Exception:
            side = empty_inventory(endpoint)
            side["coverage"] = {"identity": "failed"}
            side["limitations"].append("collection failed; provider error details were withheld")
        sides.append(side)
    return Inventory.from_dict({"schema_version": SCHEMA_VERSION, "spec_digest": digest(spec),
                                "source": sides[0], "target": sides[1]})
