"""Dispatch a target's app bindings through the unchanged, durable Helm workflow."""

from controlplane.providers.onprem.helm.adapter import HelmAdapter
from controlplane.providers.onprem.helm.runtime import HelmRuntime
from controlplane.providers.onprem.helm.worker import HelmWorker
from controlplane.providers.registry import ProviderRegistry
from controlplane.services.catalog import ApplicationCatalog


class CatalogHelmWorker:
    def __init__(self, store, runtime):
        self.base = HelmWorker(store, runtime)
        self.store, self.runtime = store, runtime
        registry = ProviderRegistry()
        registry.register(HelmAdapter(runtime.settings, store=store))
        self.catalog = ApplicationCatalog(store, registry)
        self.workers = {}

    def tick(self):
        self.base.tick()
        target = self.runtime.settings.catalog_target_id
        if not target:
            return
        # A target imported before activation keeps its public ID. Route by its
        # stable operator binding, not by the preferred new-catalog ID.
        for row in self.catalog.dynamic_bindings(
            provider_binding_id=self.runtime.settings.environment.environment_id
        ):
            worker = self.workers.get(row["id"])
            if worker is None:
                adapter = self.catalog.resolve(row["id"])
                if not adapter:
                    continue
                worker = HelmWorker(self.store, HelmRuntime(adapter.settings))
                self.workers[row["id"]] = worker
            worker.tick()

    def close(self):
        for worker in self.workers.values():
            worker.runtime.close()
