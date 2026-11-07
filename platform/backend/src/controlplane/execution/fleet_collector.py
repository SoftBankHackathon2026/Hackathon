import argparse
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from urllib3.exceptions import HTTPError as Urllib3Error

from controlplane.core import db
from controlplane.cost.collect import collect_inventory
from controlplane.cost.config import CostConfig, load_cost_config
from controlplane.cost.models import CostInventory
from controlplane.cost.onprem import OnPremMeter, create_meter
from controlplane.cost.store import CostStore
from controlplane.domain.fleet import EnvironmentConfig, FleetConfig, MigrationConfig
from controlplane.integrations.codec import (
    connector_view,
    facts_to_json,
    sample_from_status,
    sample_to_json,
)
from controlplane.integrations.config import load_fleet_config
from controlplane.integrations.http_clients import ConnectStatusClient, WorkerStatusClient
from controlplane.integrations.kube import ClusterReader
from controlplane.providers.loader import load_platform
from controlplane.repositories.fleet_journal import FleetJournal

LOG = logging.getLogger(__name__)
FACTS_EVERY_TICKS = 12
# 노드·PVC·태그 자원은 자주 바뀌지 않고 AWS API 호출 수도 있어 5초 tick 기준 약 5분마다 읽는다.
COST_EVERY_TICKS = 60
# 이 환경변수에 파일 경로가 있으면 tick 을 시작할 때마다 수정 시각을 갱신한다. 컨테이너 healthcheck 가 읽는다.
HEARTBEAT_ENV = "ONEPORT_COLLECTOR_HEARTBEAT"


class _BrokenMeter:
    """만들지 못한 측정기. 읽을 때마다 같은 오류를 내 화면에 수집 공백으로 남긴다."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def read(self, env: EnvironmentConfig):
        raise self._error


class FleetCollector:
    def __init__(
        self,
        config: FleetConfig,
        journal: FleetJournal,
        *,
        reader: Callable[[EnvironmentConfig], ClusterReader] = ClusterReader,
        costs: CostStore | None = None,
        cost_config: CostConfig | None = None,
        cost_reader: Callable[..., CostInventory] = collect_inventory,
    ) -> None:
        self._config = config
        self._journal = journal
        self._reader = reader
        self._costs = costs
        self._cost_config = cost_config or CostConfig()
        self._cost_reader = cost_reader
        self._meters: dict[str, OnPremMeter] = {}
        self._ticks = 0

    def _meter(self, env: EnvironmentConfig) -> OnPremMeter | None:
        """비용 설정이 고른 온프렘 측정기. 환경마다 한 번 만든다."""
        chosen = self._cost_config.environment(env.id).meter
        if chosen is None:
            return None
        if env.id not in self._meters:
            try:
                self._meters[env.id] = create_meter(chosen.name, chosen.options)
            except Exception as exc:  # noqa: BLE001 - 잘못된 측정기 설정이 수집 전체를 멈추지 않게 한다
                LOG.warning("env=%s meter=%s error=%s", env.id, chosen.name, type(exc).__name__)
                self._meters[env.id] = _BrokenMeter(exc)
        return self._meters[env.id]

    def collect_costs(self, env: EnvironmentConfig) -> None:
        if self._costs is None:
            return
        settings = self._cost_config.environment(env.id)
        self._costs.put(
            self._cost_reader(env, settings.tags, meter=self._meter(env), project=settings.project)
        )

    def collect_worker(self, job: MigrationConfig) -> None:
        if not job.worker_url:
            return
        at = datetime.now(UTC)
        status = WorkerStatusClient(job.worker_url).get_status()
        sample = sample_from_status(status, at)
        payload = sample_to_json(sample)
        self._journal.add_sample(job.job_id, at, payload)
        self._journal.put_worker_status(job.job_id, at, payload)

    def collect_connectors(self, job: MigrationConfig) -> None:
        if not job.connect_url:
            return
        client = ConnectStatusClient(job.connect_url)
        names = (f"{job.job_id}-source", f"{job.job_id}-sink")
        found = [(name, client.get_status(name)) for name in names]
        views = [connector_view(name, status) for name, status in found if status is not None]
        self._journal.put_connectors(job.job_id, datetime.now(UTC), views)

    def collect_environment(self, env: EnvironmentConfig) -> None:
        facts = self._reader(env).read()
        self._journal.put_facts(
            env.id, facts.observed_at or datetime.now(UTC), facts_to_json(facts)
        )

    def tick(self) -> None:
        for job in self._config.migrations:
            for collect in (self.collect_worker, self.collect_connectors):
                try:
                    collect(job)
                except httpx.HTTPError as exc:
                    LOG.warning(
                        "job=%s collect=%s error=%s",
                        job.job_id,
                        collect.__name__,
                        type(exc).__name__,
                    )
        if self._ticks % FACTS_EVERY_TICKS == 0:
            for env in self._config.environments:
                try:
                    self.collect_environment(env)
                except (ApiException, ConfigException, OSError, Urllib3Error) as exc:
                    LOG.warning("env=%s facts error=%s", env.id, type(exc).__name__)
        if self._ticks % COST_EVERY_TICKS == 0:
            for env in self._config.environments:
                try:
                    self.collect_costs(env)
                except (ApiException, ConfigException, OSError, Urllib3Error) as exc:
                    LOG.warning("env=%s cost error=%s", env.id, type(exc).__name__)
        self._ticks += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="마이그레이션 대시보드 수집기")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = load_fleet_config()
    platform = load_platform()
    if platform is None:
        parser.error("ONEPORT_DATABASE_URL_FILE(또는 ONEPORT_CONFIG)로 플랫폼 DB를 지정하세요.")
    collector = FleetCollector(
        config,
        FleetJournal(platform.store),
        costs=CostStore(platform.store),
        cost_config=load_cost_config(),
    )
    heartbeat = os.environ.get(HEARTBEAT_ENV)
    with db.pooling():
        platform.store.migrate()
        while True:
            if heartbeat:
                Path(heartbeat).touch()
            collector.tick()
            if args.once:
                return 0
            time.sleep(config.sample_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
