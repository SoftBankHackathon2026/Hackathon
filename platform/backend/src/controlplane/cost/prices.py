"""가격표. platform/scripts/fetch_cloud_prices.py 가 공식 가격 API 에서 받아 저장한 JSON 을 읽는다.

키는 클라우드와 상관없는 공통 이름이다(compute·postgres·kafka_broker·block·object 등).
화면에 보일 상품 이름은 labels 에 있다.
"""

import json
from datetime import datetime
from functools import cache
from importlib import resources

from pydantic import BaseModel, Field

from controlplane.cost.models import HOURS_PER_MONTH, PriceRef


class InstancePrice(BaseModel):
    hourly: float
    vcpu: float | None = None
    memory_gib: float | None = None


class PriceSheet(BaseModel):
    provider: str
    region: str
    currency: str
    source: str
    fetched_at: datetime
    hours_per_month: int = HOURS_PER_MONTH
    labels: dict[str, str] = Field(default_factory=dict)
    compute: dict[str, InstancePrice]
    managed: dict[str, dict[str, InstancePrice]] = Field(default_factory=dict)
    storage_gib_month: dict[str, float] = Field(default_factory=dict)
    hourly: dict[str, float] = Field(default_factory=dict)

    @property
    def ref(self) -> PriceRef:
        return PriceRef(
            provider=self.provider,
            region=self.region,
            source=self.source,
            fetched_at=self.fetched_at,
        )

    def label(self, key: str) -> str:
        return self.labels.get(key, key)

    def monthly(self, hourly: float) -> float:
        return hourly * self.hours_per_month


def available() -> list[tuple[str, str]]:
    files = resources.files("controlplane.cost") / "prices"
    return sorted(
        tuple(path.name.removesuffix(".json").split("-", 1))
        for path in files.iterdir()
        if path.name.endswith(".json")
    )


@cache
def load_sheet(provider: str, region: str) -> PriceSheet | None:
    path = resources.files("controlplane.cost") / "prices" / f"{provider}-{region}.json"
    if not path.is_file():
        return None
    return PriceSheet.model_validate(json.loads(path.read_text()))
