"""API-facing extension point. Other providers supply bindings and their own workers.

An adapter validates an intent; it never waits for a cloud operation in HTTP.
"""

from controlplane.domain.models import Runtime
from controlplane.providers.capabilities import ProviderCapabilities, provider_capabilities
from controlplane.providers.interface import DeploymentProvider


class ProviderRegistry:
    def __init__(self):
        self._bindings: dict[str, DeploymentProvider] = {}
        self._unlisted: set[str] = set()
        self.catalog = None

    def enable_catalog(self, store):
        from controlplane.services.catalog import ApplicationCatalog

        self.catalog = ApplicationCatalog(store, self)

    def register(self, adapter: DeploymentProvider, *, listed: bool = True):
        if not isinstance(adapter, DeploymentProvider):
            raise TypeError("Provider must implement the common deployment interface")
        if adapter.environment_id in self._bindings:
            raise ValueError("Duplicate environment registration")
        capabilities = adapter.capabilities()
        if capabilities.runtime != adapter.runtime:
            raise ValueError("Capability runtime does not match adapter")
        for existing in self._bindings.values():
            if existing.runtime == adapter.runtime and existing.capabilities() != capabilities:
                raise ValueError("Conflicting provider capabilities for runtime")
        self._bindings[adapter.environment_id] = adapter
        if not listed:
            self._unlisted.add(adapter.environment_id)

    def base_binding(self, environment_id: str) -> DeploymentProvider | None:
        return self._bindings.get(environment_id)

    def listed_bindings(self) -> list[tuple[str, DeploymentProvider]]:
        return [
            (key, adapter) for key, adapter in self._bindings.items() if key not in self._unlisted
        ]

    def get(self, environment_id: str) -> DeploymentProvider | None:
        if not isinstance(environment_id, str):
            return None
        return self._bindings.get(environment_id) or (
            self.catalog.resolve(environment_id) if self.catalog else None
        )

    def capabilities(self, runtime: Runtime) -> ProviderCapabilities:
        for adapter in self._bindings.values():
            if adapter.runtime == runtime:
                return adapter.capabilities()
        return provider_capabilities(runtime)

    def initialize(self):
        for adapter in self._bindings.values():
            adapter.initialize()
        if self.catalog:
            self.catalog.bootstrap()

    def runtimes(self) -> set[Runtime]:
        """Include hidden bindings without loading their app state from the database.

        Dynamic catalog bindings inherit a registered provider's runtime.
        """
        return {adapter.runtime for adapter in self._bindings.values()}

    def environments(self, *, include_unlisted: bool = False) -> list[dict]:
        items = [
            a.public_environment()
            for key, a in self._bindings.items()
            if include_unlisted or key not in self._unlisted
        ]
        if self.catalog:
            for row in self.catalog.dynamic_bindings():
                adapter = self.get(row["id"])
                if adapter:
                    items.append(adapter.public_environment())
            items = [self.catalog.decorate(item) for item in items]
        return items

    def owns(self, row: dict) -> bool:
        return self.owner(row, self.get(row["environment_id"])) is not None

    @staticmethod
    def owner(row: dict, adapter):
        """`adapter` when it is the binding that created `row`, else None."""
        if (
            adapter
            and adapter.application == row["application"]
            and adapter.runtime == row["runtime"]
        ):
            return adapter
        return None
