from psycopg.types.json import Jsonb

from controlplane.cost.models import CostInventory
from controlplane.repositories.releases import ReleaseStore


class CostStore:
    """환경별 마지막 자원 목록 하나만 둔다. 이력은 남기지 않는다."""

    def __init__(self, releases: ReleaseStore) -> None:
        self._releases = releases

    def put(self, inventory: CostInventory) -> None:
        with self._releases.connect() as conn:
            conn.execute(
                "INSERT INTO cost_inventory(env_id, inventory, observed_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(env_id) DO UPDATE SET inventory=excluded.inventory, "
                "observed_at=excluded.observed_at",
                (inventory.env_id, Jsonb(inventory.model_dump(mode="json")), inventory.observed_at),
            )

    def get(self, env_id: str) -> CostInventory | None:
        with self._releases.connect() as conn:
            row = conn.execute(
                "SELECT inventory FROM cost_inventory WHERE env_id=%s", (env_id,)
            ).fetchone()
        return CostInventory.model_validate(row["inventory"]) if row else None
