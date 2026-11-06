"""온프렘 비용 측정기를 붙이는 자리.

온프렘에는 청구서도 가격 API 도 없다. 장비비·전기 단가는 비용 설정(OnPremSpec)에 적고,
측정할 수 있는 값(소비 전력 등)은 측정기가 읽어 덮어쓴다.

측정기 붙이는 법:

    from controlplane.cost.onprem import OnPremReading, register_meter

    @register_meter("smart-plug")
    class SmartPlugMeter:
        def __init__(self, options: dict[str, Any]) -> None:
            self._url = options["url"]

        def read(self, env: EnvironmentConfig) -> OnPremReading | None:
            watts = ...  # 측정
            return OnPremReading(source="smart-plug", power_watts=watts)

비용 설정에서 고른다:

    {"environments": {"onprem": {"meter": {"name": "smart-plug", "options": {"url": "..."}}}}}

측정기 모듈은 controlplane.cost.meters 패키지 아래에 두면 자동으로 불러온다.
read() 가 None 을 돌려주거나 예외를 내면 설정값만 쓰고 화면에 수집 공백으로 남긴다.
"""

import importlib
import pkgutil
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from controlplane.domain.fleet import EnvironmentConfig


class OnPremReading(BaseModel):
    """측정기가 읽은 값. 비워 둔 필드는 비용 설정(OnPremSpec)의 값을 그대로 쓴다."""

    model_config = ConfigDict(extra="forbid")

    source: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    power_watts: float | None = Field(default=None, ge=0, description="평균 소비 전력")
    hardware_usd: float | None = Field(default=None, ge=0)
    amortization_months: int | None = Field(default=None, ge=1)
    electricity_usd_per_kwh: float | None = Field(default=None, ge=0)
    other_monthly_usd: float | None = Field(default=None, ge=0)
    note: str | None = None


class OnPremMeter(Protocol):
    def read(self, env: EnvironmentConfig) -> OnPremReading | None: ...


MeterFactory = Callable[[dict[str, Any]], OnPremMeter]
_METERS: dict[str, MeterFactory] = {}


def register_meter(name: str) -> Callable[[MeterFactory], MeterFactory]:
    def decorate(factory: MeterFactory) -> MeterFactory:
        if name in _METERS and _METERS[name] is not factory:
            raise ValueError(f"온프렘 측정기 이름 중복: {name}")
        _METERS[name] = factory
        return factory

    return decorate


def _load_bundled() -> None:
    try:
        package = importlib.import_module("controlplane.cost.meters")
    except ModuleNotFoundError:
        return
    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")


def registered() -> list[str]:
    _load_bundled()
    return sorted(_METERS)


def create_meter(name: str, options: dict[str, Any]) -> OnPremMeter:
    _load_bundled()
    if name not in _METERS:
        raise KeyError(
            f"등록되지 않은 온프렘 측정기: {name} (등록됨: {', '.join(sorted(_METERS)) or '없음'})"
        )
    return _METERS[name](options)
