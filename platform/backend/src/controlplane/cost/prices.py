"""가격표. platform/scripts/fetch_cloud_prices.py 가 공식 가격 API 에서 받아 저장한 JSON 을 읽는다.

키는 클라우드와 상관없는 공통 이름이다(compute·postgres·kafka_broker·block·object 등).
화면에 보일 상품 이름은 labels 에 있다.
"""

import json
import re
from datetime import datetime
from functools import cache
from importlib import resources

from pydantic import BaseModel, Field

from controlplane.cost.models import HOURS_PER_MONTH, PriceRef


class InstancePrice(BaseModel):
    hourly: float
    vcpu: float | None = None
    memory_gib: float | None = None


class UnitRates(BaseModel):
    """vCPU·메모리 단위 요금. 사용자 지정 머신(GCP custom)처럼 이름으로 크기가 정해지는 유형에 쓴다."""

    vcpu_hourly: float
    memory_gib_hourly: float

    def price(self, vcpu: float, memory_gib: float) -> InstancePrice:
        return InstancePrice(
            hourly=vcpu * self.vcpu_hourly + memory_gib * self.memory_gib_hourly,
            vcpu=vcpu,
            memory_gib=memory_gib,
        )


# GCP 머신 이름: <계열>-custom-<vCPU>-<메모리 MiB>, <계열>-standard|highmem|highcpu-<vCPU>
CUSTOM = re.compile(r"^(?:db-)?(?:(?P<family>[a-z0-9]+)-)?custom-(?P<vcpu>\d+)-(?P<mib>\d+)$")
PREDEFINED = re.compile(
    r"^(?P<family>[a-z0-9]+)-(?P<shape>standard|highmem|highcpu)-(?P<vcpu>\d+)$"
)
GIB_PER_VCPU = {"standard": 4, "highmem": 8, "highcpu": 1}


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
    # GB 당 데이터 전송 단가: internet_egress(인터넷 송신), inter_zone(가용 영역 간), nat_processing(NAT 처리)
    transfer_gib: dict[str, float] = Field(default_factory=dict)
    # 계열별 vCPU·메모리 단가(GCP). compute 에 없는 사용자 지정·표준 머신 이름을 이 값으로 계산한다.
    compute_rates: dict[str, UnitRates] = Field(default_factory=dict)
    # 관리형 서비스의 vCPU·메모리 단가(예: Cloud SQL db-custom-*).
    managed_rates: dict[str, UnitRates] = Field(default_factory=dict)
    # 공통 키별 주의 사항(예: 단가를 확인하지 못한 상품).
    notes: dict[str, str] = Field(default_factory=dict)

    def instance(self, name: str | None) -> InstancePrice | None:
        """인스턴스 유형 단가. 목록에 없으면 이름에서 크기를 읽어 계열 단가로 계산한다."""
        if not name:
            return None
        if name in self.compute:
            return self.compute[name]
        custom = CUSTOM.match(name)
        if custom and (rates := self.compute_rates.get(custom["family"] or "")):
            return rates.price(int(custom["vcpu"]), int(custom["mib"]) / 1024)
        predefined = PREDEFINED.match(name)
        if predefined and (rates := self.compute_rates.get(predefined["family"])):
            vcpu = int(predefined["vcpu"])
            return rates.price(vcpu, vcpu * GIB_PER_VCPU[predefined["shape"]])
        return None

    def managed_price(self, key: str, size: str | None) -> InstancePrice | None:
        """관리형 서비스 크기 단가. 목록에 없으면 db-custom-<vCPU>-<MiB> 같은 이름을 단위 요금으로 계산한다."""
        if not size:
            return None
        listed = self.managed.get(key, {}).get(size)
        if listed is not None:
            return listed
        custom = CUSTOM.match(size)
        if custom and (rates := self.managed_rates.get(key)):
            return rates.price(int(custom["vcpu"]), int(custom["mib"]) / 1024)
        return None

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
