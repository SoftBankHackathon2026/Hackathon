"""자원 목록 × 가격표 → 월 비용, 그리고 이전할 때 대상 환경에 늘어나는 비용.

모든 금액은 USD, 월 730시간 기준 온디맨드 단가다. 약정 할인·크레딧·세금·데이터 전송량은 넣지 않는다.
"""

import math
from collections import Counter
from collections.abc import Iterable

from controlplane.cost.config import EnvironmentCostConfig, MigrationCostConfig, OnPremSpec
from controlplane.cost.models import (
    HOURS_PER_MONTH,
    CostBreakdown,
    CostChange,
    CostInventory,
    CostLine,
    ManagedService,
    WorkloadDemand,
)
from controlplane.cost.onprem import OnPremReading
from controlplane.cost.prices import InstancePrice, PriceSheet

# 노드 자원 중 kubelet·시스템 데몬 몫을 빼고 파드에 줄 수 있다고 보는 비율
USABLE_CPU = 0.9
USABLE_MEMORY = 0.85
MIN_DATABASE_GIB = 20
# 온프렘 노드를 환산할 때 고르는 인스턴스 계열. 메모리가 큰 노드를 위해 r 계열도 넣는다.
DEFAULT_FAMILIES_FOR_EQUIVALENT = ("t3", "t4g", "m7g", "m6i", "r6i")
STORAGE_GROWTH = 1.2


def _money(value: float) -> float:
    return round(value, 2)


def _priced(
    category: str,
    item: str,
    quantity: float,
    unit: str,
    unit_monthly: float | None,
    note: str | None = None,
    basis: str = "list_price",
) -> CostLine:
    if unit_monthly is None:
        return CostLine(
            category=category,
            item=item,
            quantity=quantity,
            unit=unit,
            unit_price=None,
            monthly=None,
            basis="not_priced",
            note=note,
        )
    return CostLine(
        category=category,
        item=item,
        quantity=round(quantity, 2),
        unit=unit,
        unit_price=round(unit_monthly, 4),
        monthly=_money(unit_monthly * quantity),
        basis=basis,
        note=note,
    )


def family(name: str) -> str:
    """인스턴스 계열. AWS t3.medium → t3, GCP e2-standard-2 → e2."""
    return name.split(".")[0] if "." in name else name.split("-")[0]


def cheapest_fit(
    sheet: PriceSheet, vcpu: float, memory_gib: float, families: Iterable[str]
) -> tuple[str, InstancePrice] | None:
    allowed = set(families)
    fits = [
        (name, price)
        for name, price in sheet.compute.items()
        if family(name) in allowed
        and (price.vcpu or 0) >= vcpu
        and (price.memory_gib or 0) >= memory_gib
    ]
    return min(fits, key=lambda item: item[1].hourly, default=None)


def _storage_line(sheet: PriceSheet, key: str, gib: float, category: str = "storage") -> CostLine:
    price = sheet.storage_gib_month.get(key)
    return _priced(category, sheet.label(key), gib, "GiB-월", price)


def _managed_lines(sheet: PriceSheet, service: ManagedService) -> list[CostLine]:
    hourly = sheet.hourly
    if service.kind == "virtual_machine":
        price = sheet.instance(service.size)
        return [
            _priced(
                "compute",
                f"{sheet.label('compute')} {service.size} · {service.name}",
                service.count,
                "대",
                sheet.monthly(price.hourly) if price else None,
                None if price else "가격표에 없는 인스턴스 유형",
            )
        ]
    if service.kind == "block_volume":
        line = _storage_line(sheet, "block", service.storage_gib)
        return [line.model_copy(update={"item": f"{sheet.label('block_volume')} · {service.name}"})]
    if service.kind == "postgres":
        price = sheet.managed_price("postgres", service.size)
        lines = [
            _priced(
                "database",
                f"{sheet.label('postgres')} {service.size} · {service.name}",
                service.count,
                "대",
                sheet.monthly(price.hourly) if price else None,
                None if price else "가격표에 없는 인스턴스 클래스",
            )
        ]
        if service.storage_gib:
            lines.append(_storage_line(sheet, "database", service.storage_gib, "database"))
        return lines
    if service.kind == "kafka":
        price = sheet.managed_price("kafka_broker", service.size)
        rates = sheet.managed_rates.get("kafka")
        if price is None and rates is not None and service.vcpu:
            # 브로커 유형이 아니라 클러스터 vCPU·메모리로 과금하는 경우(GCP Managed Kafka)
            price = rates.price(service.vcpu, service.memory_gib)
        size = service.size or f"vCPU {service.vcpu:g} · 메모리 {service.memory_gib:g} GiB"
        note = sheet.notes.get("kafka_broker") or "가격표에 없는 브로커 유형"
        lines = [
            _priced(
                "messaging",
                f"{sheet.label('kafka_broker')} {size} · {service.name}",
                service.count,
                "브로커" if service.size else "클러스터",
                sheet.monthly(price.hourly) if price else None,
                None if price else note,
            )
        ]
        if service.storage_gib:
            lines.append(_storage_line(sheet, "kafka", service.storage_gib, "messaging"))
        return lines
    if service.kind == "object_storage":
        line = _storage_line(sheet, "object", service.storage_gib)
        return [line.model_copy(update={"item": f"{line.item} · {service.name}"})]
    key = service.kind
    price = hourly.get(key)
    return [
        _priced(
            "network" if key != "kubernetes_control_plane" else "control_plane",
            f"{sheet.label(key)} · {service.name}",
            service.count,
            "개",
            sheet.monthly(price) if price is not None else None,
            (sheet.notes.get(key) or "사용량(처리 GB 등) 요금 제외")
            if key in ("load_balancer", "nat_gateway")
            else None,
        )
    ]


def price_cloud(inventory: CostInventory, sheet: PriceSheet) -> CostBreakdown:
    """클라우드 환경의 지금 월 비용: 노드 + 컨트롤 플레인 + 볼륨 + 태그로 찾은 관리형 서비스."""
    lines: list[CostLine] = []
    for itype, count in sorted(Counter(n.instance_type for n in inventory.nodes).items(), key=str):
        price = sheet.instance(itype)
        lines.append(
            _priced(
                "compute",
                f"{sheet.label('compute')} {itype or '유형 미상'}",
                count,
                "대",
                sheet.monthly(price.hourly) if price else None,
                None if price else "가격표에 없는 인스턴스 유형",
            )
        )
    if inventory.kubernetes_control_plane and not any(
        s.kind == "kubernetes_control_plane" for s in inventory.managed
    ):
        price = sheet.hourly.get("kubernetes_control_plane")
        lines.append(
            _priced(
                "control_plane",
                sheet.label("kubernetes_control_plane"),
                1,
                "개",
                sheet.monthly(price) if price is not None else None,
            )
        )
    volume_gib = sum(v.gib for v in inventory.volumes)
    if volume_gib:
        lines.append(_storage_line(sheet, "block", volume_gib))
    for service in inventory.managed:
        lines += _managed_lines(sheet, service)
    if inventory.public_ipv4:
        price = sheet.hourly.get("public_ipv4")
        lines.append(
            _priced(
                "network",
                sheet.label("public_ipv4"),
                inventory.public_ipv4,
                "개",
                sheet.monthly(price) if price is not None else None,
            )
        )
    lines += transfer_lines(inventory, sheet)
    return CostBreakdown.of("지금 월 비용", lines)


def transfer_unit(sheet: PriceSheet, kind: str) -> float | None:
    """GB 당 전송 단가. ALB 처리량은 1 GB 를 1 LCU-시간으로 본다."""
    if kind == "lb_processing":
        return sheet.hourly.get("load_balancer_capacity_unit")
    return sheet.transfer_gib.get(kind)


def transfer_lines(inventory: CostInventory, sheet: PriceSheet) -> list[CostLine]:
    lines = []
    for usage in inventory.transfers:
        unit = transfer_unit(sheet, usage.kind)
        lines.append(
            _priced(
                "transfer",
                f"{sheet.label(usage.kind)} · {usage.name}",
                usage.gib_per_month,
                "GB/월",
                unit,
                usage.note if unit is not None else (sheet.notes.get("transfer") or "단가 없음"),
            )
        )
    return lines


def _pick(field: str, spec: OnPremSpec, reading: OnPremReading | None) -> tuple[object, str]:
    """측정값이 있으면 측정값, 없으면 운영자 입력값. (값, 근거)."""
    measured = getattr(reading, field) if reading is not None else None
    if measured is not None:
        return measured, "measured"
    return getattr(spec, field), "owner_input"


def price_onprem(spec: OnPremSpec, reading: OnPremReading | None = None) -> CostBreakdown:
    """온프렘 실제 비용(TCO): 장비 감가상각 + 전기 + 기타. 값이 없는 줄은 '입력 필요'.

    측정기(controlplane.cost.onprem)가 읽은 값이 있으면 그 필드만 설정값 대신 쓴다.
    """
    hardware_usd, hardware_basis = _pick("hardware_usd", spec, reading)
    months, months_basis = _pick("amortization_months", spec, reading)
    watts, watts_basis = _pick("power_watts", spec, reading)
    rate, rate_basis = _pick("electricity_usd_per_kwh", spec, reading)
    other, other_basis = _pick("other_monthly_usd", spec, reading)
    hardware = hardware_usd / months if hardware_usd is not None else None
    power_unit = HOURS_PER_MONTH / 1000 * rate if watts is not None and rate is not None else None
    missing = "입력 필요"
    measured = "measured" in (watts_basis, rate_basis)
    lines = [
        _priced(
            "onprem",
            f"장비 감가상각 ({months}개월)",
            1,
            "월",
            hardware,
            None if hardware is not None else missing,
            "measured" if "measured" in (hardware_basis, months_basis) else "owner_input",
        ),
        _priced(
            "onprem",
            "전기",
            watts or 0,
            "W 평균",
            power_unit,
            (f"측정: {reading.source}" if measured and reading else None)
            if power_unit is not None
            else missing,
            "measured" if measured else "owner_input",
        ),
    ]
    if other:
        lines.append(_priced("onprem", "회선·공간 등", 1, "월", other, None, other_basis))
    label = "지금 월 비용 (측정값 포함)" if reading is not None else "지금 월 비용 (운영자 입력)"
    return CostBreakdown.of(label, lines)


def price_equivalent(
    inventory: CostInventory, sheet: PriceSheet, families: Iterable[str]
) -> CostBreakdown:
    """온프렘 노드를 같은 스펙 이상의 가장 싼 클라우드 인스턴스로 바꿨을 때의 월 비용."""
    families = list(families)
    lines = []
    for node in inventory.nodes:
        fit = cheapest_fit(sheet, node.vcpu, node.memory_gib, families)
        lines.append(
            _priced(
                "compute",
                f"{node.name} → {fit[0] if fit else '맞는 유형 없음'}",
                1,
                "대",
                sheet.monthly(fit[1].hourly) if fit else None,
                f"{node.vcpu:g} vCPU · {node.memory_gib:.1f} GiB 이상",
            )
        )
    volume_gib = sum(v.gib for v in inventory.volumes)
    if volume_gib:
        lines.append(_storage_line(sheet, "block", volume_gib))
    return CostBreakdown.of(f"{sheet.provider.upper()} {sheet.region} 동급 환산", lines)


def price_environment(
    inventory: CostInventory, sheet: PriceSheet | None, config: EnvironmentCostConfig
) -> tuple[CostBreakdown | None, CostBreakdown | None]:
    """(지금 월 비용, 클라우드 동급 환산). 온프렘만 두 번째 값이 있다."""
    if inventory.provider == "onprem":
        equivalent = (
            price_equivalent(inventory, sheet, DEFAULT_FAMILIES_FOR_EQUIVALENT) if sheet else None
        )
        return price_onprem(config.onprem, inventory.onprem_reading), equivalent
    return (price_cloud(inventory, sheet) if sheet else None), None


def _free_capacity(inventory: CostInventory) -> tuple[float, float]:
    cpu = sum(max(0.0, n.vcpu * USABLE_CPU - n.requested_vcpu) for n in inventory.nodes)
    memory = sum(
        max(0.0, n.memory_gib * USABLE_MEMORY - n.requested_memory_gib) for n in inventory.nodes
    )
    return cpu, memory


def size_nodes(
    sheet: PriceSheet,
    vcpu: float,
    memory_gib: float,
    families: Iterable[str],
    minimum: int,
    exact: str | None = None,
) -> tuple[str, int, float] | None:
    """필요한 vCPU·메모리를 담는 노드 묶음 중 월 비용이 가장 싼 (유형, 대수, 월 비용).

    exact 를 주면 그 유형 하나로만 센다(기존 노드 그룹 확장).
    """
    allowed = set(families)
    best = None
    for name, price in sheet.compute.items():
        if exact is not None and name != exact:
            continue
        if exact is None and family(name) not in allowed:
            continue
        if not price.vcpu or not price.memory_gib:
            continue
        count = max(
            minimum,
            math.ceil(vcpu / (price.vcpu * USABLE_CPU)),
            math.ceil(memory_gib / (price.memory_gib * USABLE_MEMORY)),
        )
        cost = sheet.monthly(price.hourly) * count
        if best is None or cost < best[2]:
            best = (name, count, cost)
    return best


def _cheapest_postgres(sheet: PriceSheet, memory_gib: float) -> str | None:
    classes = sheet.managed.get("postgres", {})
    fits = [(n, p) for n, p in classes.items() if (p.memory_gib or 0) >= memory_gib]
    return min(fits, key=lambda item: item[1].hourly, default=(None, None))[0]


def _cheapest_broker(sheet: PriceSheet) -> str | None:
    brokers = sheet.managed.get("kafka_broker", {})
    return min(brokers, key=lambda name: brokers[name].hourly, default=None)


def managed_equivalents(
    demands: list[WorkloadDemand], sheet: PriceSheet, headroom: float
) -> list[ManagedService]:
    """원본 데이터 워크로드를 대상 클라우드 관리형 서비스로 바꾼 목록."""
    services = []
    for demand in (d for d in demands if d.role == "postgres"):
        memory = max(1.0, demand.memory_gib / max(demand.replicas, 1) * (1 + headroom))
        services.append(
            ManagedService(
                kind="postgres",
                name=f"{demand.namespace}/{demand.name}",
                size=_cheapest_postgres(sheet, memory),
                storage_gib=max(MIN_DATABASE_GIB, math.ceil(demand.storage_gib * STORAGE_GROWTH)),
            )
        )
    kafka = [d for d in demands if d.role == "kafka"]
    if kafka:
        brokers = [d for d in kafka if "broker" in d.name] or kafka
        services.append(
            ManagedService(
                kind="kafka",
                name=", ".join(sorted({f"{d.namespace}/{d.name}" for d in kafka})),
                size=_cheapest_broker(sheet),
                count=max(2, sum(d.replicas for d in brokers)),
                storage_gib=math.ceil(sum(d.storage_gib for d in kafka) * STORAGE_GROWTH),
            )
        )
    for demand in (d for d in demands if d.role == "object_storage"):
        services.append(
            ManagedService(
                kind="object_storage",
                name=f"{demand.namespace}/{demand.name}",
                storage_gib=max(1, math.ceil(demand.storage_gib)),
            )
        )
    return services


DATA_ROLES = ("postgres", "kafka", "object_storage")


def node_demand(
    source: CostInventory, namespaces: list[str], config: MigrationCostConfig
) -> tuple[list[WorkloadDemand], list[WorkloadDemand], float, float]:
    """(옮길 워크로드, 그중 노드에 올라갈 것, 필요 vCPU, 필요 메모리 GiB). 여유율 포함."""
    demands = [d for d in source.workloads if d.namespace in namespaces]
    managed = config.data_strategy == "managed"
    on_nodes = [d for d in demands if not (managed and d.role in DATA_ROLES)]
    need_cpu = sum(d.vcpu for d in on_nodes) * (1 + config.headroom)
    need_memory = sum(d.memory_gib for d in on_nodes) * (1 + config.headroom)
    return demands, on_nodes, need_cpu, need_memory


def capacity_assumptions(
    need_cpu: float, need_memory: float, free_cpu: float, free_memory: float, headroom: float
) -> list[str]:
    return [
        f"파드 요청량(requests) 합계 vCPU {need_cpu:.2f} · 메모리 {need_memory:.1f} GiB "
        f"(여유율 {headroom:.0%} 포함)",
        f"대상 노드의 남는 용량 vCPU {free_cpu:.2f} · 메모리 {free_memory:.1f} GiB 먼저 사용",
    ]


def free_capacity(inventory: CostInventory | None) -> tuple[float, float]:
    return _free_capacity(inventory) if inventory is not None and inventory.nodes else (0.0, 0.0)


def _existing_type(target: CostInventory, sheet: PriceSheet) -> str | None:
    """대상 클러스터가 이미 쓰는 노드 유형(가장 많은 것). 같은 노드 그룹을 늘린다고 본다."""
    types = Counter(n.instance_type for n in target.nodes if sheet.instance(n.instance_type))
    return types.most_common(1)[0][0] if types else None


ROLE_LABELS = {
    "postgres": "자가 운영 PostgreSQL",
    "kafka": "자가 운영 Kafka",
    "object_storage": "자가 운영 오브젝트 스토리지",
}


def _sum(lines: list[CostLine]) -> float | None:
    if any(line.monthly is None for line in lines):
        return None
    return round(sum(line.monthly or 0 for line in lines), 2)


def _workload_summary(demands: list[WorkloadDemand]) -> str:
    vcpu = sum(d.vcpu for d in demands)
    memory = sum(d.memory_gib for d in demands)
    return f"워크로드 {len(demands)}개 · 요청 vCPU {vcpu:.2f} · 메모리 {memory:.1f} GiB"


def _data_source(service: ManagedService, demands: list[WorkloadDemand]) -> str:
    names = [n.strip() for n in service.name.split(",")]
    matched = [d for d in demands if f"{d.namespace}/{d.name}" in names]
    gib = sum(d.storage_gib for d in matched)
    replicas = sum(d.replicas for d in matched)
    label = ROLE_LABELS.get(service.kind, service.kind)
    pods = f" · 파드 {replicas}개" if replicas > 1 else ""
    return (
        f"{', '.join(d.name for d in matched) or service.name} ({label}{pods} · 디스크 {gib:g} GiB)"
    )


def _target_label(lines: list[CostLine], service: ManagedService) -> str:
    head = lines[0].item.split(" · ")[0]
    if service.count > 1 and lines[0].unit in ("대", "브로커"):
        head = f"{head} × {service.count}"
    storage = sum(line.quantity for line in lines[1:])
    if service.kind == "object_storage":
        return f"{head} {service.storage_gib:g} GiB"
    return f"{head} + 스토리지 {storage:g} GiB" if storage else head


def estimate_added(
    source: CostInventory,
    target: CostInventory | None,
    sheet: PriceSheet,
    namespaces: list[str],
    config: MigrationCostConfig,
) -> tuple[CostBreakdown, list[str], list[CostChange]]:
    """원본 네임스페이스 워크로드를 클라우드 대상에 옮길 때 대상에서 늘어나는 월 비용.

    (늘어나는 비용 줄, 가정, 원본 자원 → 대상 자원 변화 행)
    """
    demands, on_nodes, need_cpu, need_memory = node_demand(source, namespaces, config)
    managed = config.data_strategy == "managed"
    has_cluster = target is not None and bool(target.nodes)
    free_cpu, free_memory = free_capacity(target)
    extra_cpu = max(0.0, need_cpu - free_cpu)
    extra_memory = max(0.0, need_memory - free_memory)
    assumptions = [
        *capacity_assumptions(need_cpu, need_memory, free_cpu, free_memory, config.headroom),
        "데이터 서비스는 관리형으로 바꿈"
        if managed
        else "데이터 서비스는 대상 클러스터에 그대로 띄움",
        "온디맨드 단가, 월 730시간. 약정 할인·크레딧·세금 제외",
    ]
    lines: list[CostLine] = []
    changes: list[CostChange] = []
    if not has_cluster:
        price = sheet.hourly.get("kubernetes_control_plane")
        line = _priced(
            "control_plane",
            sheet.label("kubernetes_control_plane"),
            1,
            "개",
            sheet.monthly(price) if price is not None else None,
            "대상 클러스터가 없어 새로 만든다고 봄",
        )
        lines.append(line)
        changes.append(
            CostChange(
                change="added",
                category="control_plane",
                source=None,
                target=line.item,
                before=None,
                after=line.monthly,
                delta=line.monthly,
                note=line.note,
            )
        )
    compute: list[CostLine] = []
    if extra_cpu > 0 or extra_memory > 0:
        existing = _existing_type(target, sheet) if has_cluster else None
        if existing:
            sized = size_nodes(sheet, extra_cpu, extra_memory, [existing], 0, exact=existing)
        else:
            minimum = 0 if has_cluster else config.min_nodes
            sized = size_nodes(sheet, extra_cpu, extra_memory, config.instance_families, minimum)
        if sized:
            name, count, _ = sized
            compute.append(
                _priced(
                    "compute",
                    f"{sheet.label('compute')} {name} 추가",
                    count,
                    "대",
                    sheet.monthly(sheet.instance(name).hourly),
                    f"부족분 vCPU {extra_cpu:.2f} · 메모리 {extra_memory:.1f} GiB"
                    + (" · 기존 노드와 같은 유형" if existing else ""),
                )
            )
    lines += compute
    if on_nodes:
        target_text = "기존 노드의 남는 용량"
        if compute:
            target_text = f"남는 용량 + {compute[0].item.removesuffix(' 추가')} {compute[0].quantity:g}대 추가"
        after = _sum(compute) if compute else 0.0
        changes.append(
            CostChange(
                change="moved",
                category="compute",
                source=_workload_summary(on_nodes),
                target=target_text,
                before=None,
                after=after,
                delta=after,
                note=compute[0].note if compute else "대상 노드에 여유가 있어 노드를 늘리지 않음",
            )
        )
    kept_gib = sum(d.storage_gib for d in on_nodes)
    if kept_gib:
        line = _storage_line(sheet, "block", kept_gib)
        lines.append(line)
        changes.append(
            CostChange(
                change="moved",
                category="storage",
                source=f"PVC 볼륨 {kept_gib:g} GiB",
                target=f"{line.item} {kept_gib:g} GiB",
                before=None,
                after=line.monthly,
                delta=line.monthly,
            )
        )
    if managed:
        for service in managed_equivalents(demands, sheet, config.headroom):
            service_lines = _managed_lines(sheet, service)
            lines += service_lines
            after = _sum(service_lines)
            changes.append(
                CostChange(
                    change="replaced",
                    category=service_lines[0].category,
                    source=_data_source(service, demands),
                    target=_target_label(service_lines, service),
                    before=None,
                    after=after,
                    delta=after,
                    note=next((line.note for line in service_lines if line.note), None),
                )
            )
    return CostBreakdown.of("이전으로 늘어나는 월 비용", lines), assumptions, changes


def onprem_changes(
    source: CostInventory, namespaces: list[str], config: MigrationCostConfig
) -> list[CostChange]:
    """온프렘으로 옮길 때: 단가가 없으므로 남는 용량에 들어가면 월 비용 변화 0."""
    demands = [d for d in source.workloads if d.namespace in namespaces]
    if not demands:
        return []
    return [
        CostChange(
            change="moved",
            category="compute",
            source=_workload_summary(demands),
            target="온프렘 노드의 남는 용량",
            before=None,
            after=0.0,
            delta=0.0,
            note="온프렘 장비비는 이전과 상관없이 그대로",
        )
    ]


def one_time_costs(
    source: CostInventory,
    target_provider: str,
    source_sheet: PriceSheet | None,
    namespaces: list[str],
    config: MigrationCostConfig,
    monthly_increase: float | None,
) -> tuple[CostBreakdown, list[str]]:
    """이전할 때 한 번 드는 비용: 데이터 첫 복사, 전환 기간 CDC 변경분 전송, 이중 운영."""
    moving_gib = sum(d.storage_gib for d in source.workloads if d.namespace in namespaces)
    cdc_gib = config.cdc_gib_per_day * config.dual_run_days
    if source.provider == "onprem":
        unit, note = 0.0, "클라우드로 들어오는 전송은 무료"
    else:
        unit = source_sheet.transfer_gib.get("internet_egress") if source_sheet else None
        note = (
            "원본 클라우드에서 인터넷으로 나가는 송신 요금"
            if unit is not None
            else "송신 단가 없음"
        )
    lines = [
        _priced(
            "transfer", "데이터 첫 복사", moving_gib, "GB", unit, f"PVC 용량 기준(상한) · {note}"
        ),
        _priced(
            "transfer",
            f"전환 기간 변경분(CDC) {config.dual_run_days}일",
            cdc_gib,
            "GB",
            unit,
            f"하루 {config.cdc_gib_per_day:g} GB 가정 · {note}",
        ),
    ]
    if monthly_increase is not None:
        lines.append(
            _priced(
                "compute",
                f"이중 운영 {config.dual_run_days}일",
                config.dual_run_days,
                "일",
                monthly_increase / 30,
                "전환을 마치기 전까지 대상 증가분을 원본과 함께 냄",
            )
        )
    assumptions = [
        f"데이터 첫 복사 {moving_gib:g} GB, 전환 기간 변경분 하루 {config.cdc_gib_per_day:g} GB"
    ]
    return CostBreakdown.of("1회성 비용", lines), assumptions


def released_monthly(
    source: CostInventory, sheet: PriceSheet | None, namespaces: list[str]
) -> tuple[float | None, str]:
    """옮긴 뒤 원본에서 줄어드는 월 비용과 그 근거.

    클라우드 원본: 노드 비용 × 옮기는 워크로드의 요청량 비중 + 그 네임스페이스 볼륨. 컨트롤 플레인·LB·
    관리형 서비스처럼 함께 쓰는 자원은 남는다고 본다. 온프렘 원본: 장비는 그대로 두므로 0.
    """
    if source.provider == "onprem":
        return 0.0, "원본 온프렘 장비는 이전 뒤에도 그대로 둔다고 봄(줄어드는 비용 0)"
    if sheet is None:
        return None, "원본 가격표 없음"
    total_cpu = sum(n.requested_vcpu for n in source.nodes)
    total_memory = sum(n.requested_memory_gib for n in source.nodes)
    moving = [d for d in source.workloads if d.namespace in namespaces]
    shares = []
    if total_cpu:
        shares.append(sum(d.vcpu for d in moving) / total_cpu)
    if total_memory:
        shares.append(sum(d.memory_gib for d in moving) / total_memory)
    share = min(1.0, sum(shares) / len(shares)) if shares else 0.0
    nodes = sum(
        sheet.monthly(price.hourly)
        for n in source.nodes
        if (price := sheet.instance(n.instance_type))
    )
    block = sheet.storage_gib_month.get("block", 0.0) * sum(
        v.gib for v in source.volumes if v.owner.split("/", 1)[0] in namespaces
    )
    return (
        round(nodes * share + block, 2),
        f"원본에서 줄어드는 비용: 노드 비용의 {share:.0%}(요청량 비중) + 볼륨. "
        "컨트롤 플레인·LB·관리형 서비스는 남는다고 봄",
    )
