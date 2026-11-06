from collections.abc import Callable
from datetime import UTC, datetime

from controlplane.cost.config import CostConfig
from controlplane.cost.estimator import (
    capacity_assumptions,
    estimate_added,
    free_capacity,
    node_demand,
    one_time_costs,
    onprem_changes,
    price_environment,
    released_monthly,
)
from controlplane.cost.models import (
    CapacityCheck,
    CostChange,
    CostInventory,
    CostOverview,
    EnvironmentCost,
    MigrationCost,
)
from controlplane.cost.prices import PriceSheet, load_sheet
from controlplane.domain.fleet import EnvironmentConfig, FleetConfig, MigrationConfig

InventoryLookup = Callable[[str], CostInventory | None]


class CostService:
    """환경별 지금 비용과 이전 작업별 대상 환경 추산.

    가격표: 클라우드 환경은 자기 provider·region, 온프렘은 비용 설정의 equivalent(기본 AWS 서울).
    """

    def __init__(
        self,
        fleet: FleetConfig,
        config: CostConfig,
        inventory: InventoryLookup,
        sheets: Callable[[str, str], PriceSheet | None] = load_sheet,
    ) -> None:
        self._fleet = fleet
        self._config = config
        self._inventory = inventory
        self._sheets = sheets

    def _sheet(self, env: EnvironmentConfig) -> PriceSheet | None:
        if env.provider == "onprem":
            basis = self._config.environment(env.id).equivalent
            return self._sheets(basis.provider, basis.region)
        return self._sheets(env.provider, env.region)

    def environment_cost(self, env: EnvironmentConfig) -> EnvironmentCost:
        inventory = self._inventory(env.id)
        sheet = self._sheet(env)
        gaps = [] if sheet else [f"가격표 없음: {env.provider} {env.region}"]
        if inventory is None:
            return EnvironmentCost(
                env_id=env.id,
                name=env.name,
                provider=env.provider,
                observed_at=None,
                current=None,
                prices=sheet.ref if sheet else None,
                gaps=[*gaps, "자원 목록을 아직 수집하지 않음"],
            )
        current, equivalent = price_environment(inventory, sheet, self._config.environment(env.id))
        return EnvironmentCost(
            env_id=env.id,
            name=env.name,
            provider=env.provider,
            observed_at=inventory.observed_at,
            current=current,
            cloud_equivalent=equivalent,
            prices=sheet.ref if sheet else None,
            sources=inventory.sources,
            gaps=[*gaps, *inventory.gaps],
        )

    def _source_monthly(self, cost: EnvironmentCost) -> float | None:
        """원본 월 비용. 온프렘 입력값이 비어 있으면 클라우드 동급 환산값을 쓴다."""
        if cost.current and cost.current.unpriced == 0:
            return cost.current.monthly
        if cost.cloud_equivalent:
            return cost.cloud_equivalent.monthly
        return cost.current.monthly if cost.current else None

    def migration_cost(
        self, job: MigrationConfig, costs: dict[str, EnvironmentCost]
    ) -> MigrationCost:
        settings = self._config.migration(job.job_id)
        source_env = self._fleet.environment(job.source_env)
        target_env = self._fleet.environment(job.target_env)
        source = self._inventory(source_env.id)
        target = self._inventory(target_env.id)
        sheet = self._sheet(target_env)
        source_cost, target_cost = costs[source_env.id], costs[target_env.id]
        source_monthly = self._source_monthly(source_cost)
        target_now = target_cost.current.monthly if target_cost.current else None
        gaps = []
        if source is None:
            gaps.append("원본 자원 목록 없음")
        if sheet is None and target_env.provider != "onprem":
            gaps.append(f"대상 가격표 없음: {target_env.provider} {target_env.region}")
        added = None
        changes: list[CostChange] = []
        assumptions: list[str] = []
        capacity = None
        namespaces = settings.namespaces or [source_env.app_namespace]
        if source is not None:
            _, _, need_cpu, need_memory = node_demand(source, namespaces, settings)
            free_cpu, free_memory = free_capacity(target)
            capacity = CapacityCheck(
                need_vcpu=round(need_cpu, 2),
                need_memory_gib=round(need_memory, 2),
                free_vcpu=round(free_cpu, 2),
                free_memory_gib=round(free_memory, 2),
            )
            if target_env.provider == "onprem":
                # 온프렘은 단가가 없다. 남는 용량 안에 들어가면 월 비용은 그대로, 아니면 증설이 필요하다.
                assumptions = capacity_assumptions(
                    need_cpu, need_memory, free_cpu, free_memory, settings.headroom
                )
                changes = onprem_changes(source, namespaces, settings)
                if not capacity.fits:
                    gaps.append(
                        "온프렘 용량 부족: 장비 증설 필요 "
                        f"(vCPU {max(0, need_cpu - free_cpu):.2f} · "
                        f"메모리 {max(0, need_memory - free_memory):.1f} GiB)"
                    )
            elif sheet is not None:
                added, assumptions, changes = estimate_added(
                    source, target, sheet, namespaces, settings
                )
            assumptions = [f"옮길 네임스페이스: {', '.join(namespaces)}", *assumptions]
        if source_cost.current and source_cost.current.unpriced and source_cost.cloud_equivalent:
            gaps.append("원본 온프렘 장비비·전기료 미입력: 원본 비용은 클라우드 동급 환산값")
        released = None
        source_sheet = self._sheet(source_env)
        if source is not None:
            released, why = released_monthly(source, source_sheet, namespaces)
            assumptions.append(why)
            if released:
                changes.append(
                    CostChange(
                        change="removed",
                        category="compute",
                        source=f"{source_env.name} 노드·볼륨 중 옮기는 몫",
                        target=None,
                        before=released,
                        after=0.0,
                        delta=-released,
                        note=why,
                    )
                )
        increase = added.monthly if added else (0.0 if target_env.provider == "onprem" else None)
        after = round((target_now or 0) + increase, 2) if increase is not None else None
        change = (
            round(increase - released, 2) if increase is not None and released is not None else None
        )
        dual_run = (
            round(increase * settings.dual_run_days / 30, 2) if increase is not None else None
        )
        one_time = None
        if source is not None:
            one_time, one_time_why = one_time_costs(
                source, target_env.provider, source_sheet, namespaces, settings, increase
            )
            assumptions += one_time_why
        changes.sort(key=lambda c: -abs(c.delta or 0))
        return MigrationCost(
            job_id=job.job_id,
            source_env=source_env.id,
            target_env=target_env.id,
            source_monthly=source_monthly,
            target_monthly_now=target_now,
            target_monthly_after=after,
            added=added,
            source_released=released,
            monthly_change=change,
            capacity=capacity,
            capacity_fits=capacity.fits if capacity else None,
            changes=changes,
            one_time=one_time,
            dual_run_days=settings.dual_run_days,
            dual_run_cost=dual_run,
            assumptions=assumptions or [],
            gaps=gaps,
        )

    def overview(self) -> CostOverview:
        costs = {env.id: self.environment_cost(env) for env in self._fleet.environments}
        return CostOverview(
            generated_at=datetime.now(UTC),
            environments=list(costs.values()),
            migrations=[self.migration_cost(job, costs) for job in self._fleet.migrations],
        )
