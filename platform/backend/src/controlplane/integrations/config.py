from controlplane.core.config import PlatformEnvironment
from controlplane.domain.fleet import FleetConfig
from controlplane.integrations.defaults import DEFAULT_FLEET


def load_fleet_config(environment: PlatformEnvironment | None = None) -> FleetConfig:
    path = (environment or PlatformEnvironment()).fleet_config
    if path is None:
        return DEFAULT_FLEET
    return FleetConfig.model_validate_json(path.read_text())
