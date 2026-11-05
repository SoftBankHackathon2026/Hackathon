from controlplane.domain.models import Step
from controlplane.providers.capabilities import provider_capabilities
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.onprem.telemetry import OnpremMonitoring


def provider_step(step_id: str, title: str, detail: str) -> Step:
    return Step(id=step_id, title=title, owner="provider", detail=detail)


class OnpremAdapterBase(OnpremMonitoring, DurableDeploymentProvider):
    supports_restore = True

    def __init__(self, settings, *, store=None):
        super().__init__(settings, store=store)
        self.application = settings.environment.application
        self.environment_id = settings.environment.environment_id

    def capabilities(self):
        return provider_capabilities(self.runtime)

    def fingerprint(self):
        return self.settings.fingerprint()

    def environment_summary(self) -> dict:
        env = self.settings.environment
        return {
            "id": self.environment_id,
            "application": self.application,
            "runtime": self.runtime,
            "release_restore": self.supports_restore,
            "state": "registered",
            "executable": True,
            "input_kind": "image",
            "connection_verified": False,
            "monitoring_enabled": self.settings.observability is not None,
            "max_replicas": env.max_replicas,
            "max_cpu": env.max_cpu,
            "max_memory_mib": env.max_memory_mib,
            "service_url": f"https://{env.hostname}",
        }
