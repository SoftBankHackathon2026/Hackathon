import argparse
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from kubernetes.config import ConfigException

from controlplane.core import db
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


class FleetCollector:
    def __init__(
        self,
        config: FleetConfig,
        journal: FleetJournal,
        *,
        reader: Callable[[EnvironmentConfig], ClusterReader] = ClusterReader,
    ) -> None:
        self._config = config
        self._journal = journal
        self._reader = reader
        self._ticks = 0

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
                except (ConfigException, OSError) as exc:
                    LOG.warning("env=%s facts error=%s", env.id, type(exc).__name__)
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
    collector = FleetCollector(config, FleetJournal(platform.store))
    with db.pooling():
        platform.store.migrate()
        while True:
            collector.tick()
            if args.once:
                return 0
            time.sleep(config.sample_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
