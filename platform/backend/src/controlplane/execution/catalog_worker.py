"""Discover app bindings without restarting the target's existing worker process."""

import logging

from controlplane.providers.registry import ProviderRegistry
from controlplane.services.catalog import ApplicationCatalog

LOG = logging.getLogger(__name__)


class CatalogWorker:
    def __init__(self, store, runtime, *, adapter_type, runtime_type, worker_type):
        self.store, self.runtime = store, runtime
        self.runtime_type, self.worker_type = runtime_type, worker_type
        self.base = worker_type(store, runtime)
        base = adapter_type(runtime.settings, store=store)
        self.binding_id = base.environment_id
        registry = ProviderRegistry()
        registry.register(base)
        self.catalog = ApplicationCatalog(store, registry)
        self.workers = {}

    def tick(self):
        # One broken app must not starve other apps sharing the target.
        try:
            self.base.tick()
        except Exception as exc:  # noqa: BLE001 -- leave durable journals for the next tick
            LOG.error("binding=%s exception=%s", self.binding_id, type(exc).__name__)
        # Disabling new creation must not abandon existing dynamic releases/deletions.
        for row in self.catalog.dynamic_bindings(provider_binding_id=self.binding_id):
            try:
                worker = self.workers.get(row["id"])
                if worker is None:
                    adapter = self.catalog.resolve(row["id"])
                    if adapter is None:
                        continue
                    worker = self.worker_type(self.store, self.runtime_type(adapter.settings))
                    self.workers[row["id"]] = worker
                worker.tick()
            except Exception as exc:  # noqa: BLE001 -- no credential/payload logging
                LOG.error("binding=%s exception=%s", row["id"], type(exc).__name__)

    def close(self):
        for worker in self.workers.values():
            worker.runtime.close()


class CatalogGcpWorker(CatalogWorker):
    def __init__(self, store, runtime):
        from controlplane.execution.gcp.runtime import GcpRuntime
        from controlplane.execution.gcp.worker import GcpWorker
        from controlplane.providers.gcp.adapter import GcpAdapter

        super().__init__(
            store, runtime, adapter_type=GcpAdapter, runtime_type=GcpRuntime, worker_type=GcpWorker
        )


class CatalogAwsWorker(CatalogWorker):
    def __init__(self, store, runtime):
        from oneport_aws.adapter import AwsAdapter
        from oneport_aws.execution.oneport_ecs_runtime import OneportEcsRuntime
        from oneport_aws.execution.oneport_ecs_worker import OneportEcsWorker

        super().__init__(
            store,
            runtime,
            adapter_type=AwsAdapter,
            runtime_type=OneportEcsRuntime,
            worker_type=OneportEcsWorker,
        )
