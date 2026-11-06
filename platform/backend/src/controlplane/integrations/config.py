from controlplane.core.config import PlatformEnvironment
from controlplane.domain.fleet import EnvironmentConfig, FleetConfig
from controlplane.integrations.defaults import DEFAULT_FLEET

DEFAULT_ENVIRONMENTS = {env.id: env for env in DEFAULT_FLEET.environments}


def with_default_layout(env: EnvironmentConfig) -> EnvironmentConfig:
    preset = DEFAULT_ENVIRONMENTS.get(env.id)
    if preset is None:
        return env
    update: dict[str, object] = {}
    if not env.components:
        update["components"] = [c for c in preset.components if not (c.workload or c.argo_app)]
    if not env.boundary:
        update["boundary"] = preset.boundary
    if not env.cluster_path:
        update["cluster_path"] = preset.cluster_path
    return env.model_copy(update=update)


def load_fleet_config(environment: PlatformEnvironment | None = None) -> FleetConfig:
    path = (environment or PlatformEnvironment()).fleet_config
    if path is None:
        return DEFAULT_FLEET
    config = FleetConfig.model_validate_json(path.read_text())
    return config.model_copy(
        update={"environments": [with_default_layout(env) for env in config.environments]}
    )
