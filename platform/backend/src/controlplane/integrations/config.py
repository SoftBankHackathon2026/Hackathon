from controlplane.core.config import PlatformEnvironment
from controlplane.domain.fleet import EnvironmentConfig, FleetConfig
from controlplane.integrations.defaults import DEFAULT_FLEET

DEFAULT_ENVIRONMENTS = {env.id: env for env in DEFAULT_FLEET.environments}


def with_default_layout(env: EnvironmentConfig) -> EnvironmentConfig:
    """설정에 구성 요소가 없으면 같은 id 의 기본 환경 구성(아키텍처 배치)을 쓴다.

    실측 설정 파일은 연결 정보와 보안 사실만 적고, 화면에 그릴 구성 요소·배치는 기본값을 이어 받는다.
    구성 요소를 직접 적으면 그 값을 그대로 쓴다.
    """
    preset = DEFAULT_ENVIRONMENTS.get(env.id)
    if env.components or preset is None:
        return env
    update = {"components": preset.components}
    if not env.boundary:
        update["boundary"] = preset.boundary
    return env.model_copy(update=update)


def load_fleet_config(environment: PlatformEnvironment | None = None) -> FleetConfig:
    path = (environment or PlatformEnvironment()).fleet_config
    if path is None:
        return DEFAULT_FLEET
    config = FleetConfig.model_validate_json(path.read_text())
    return config.model_copy(
        update={"environments": [with_default_layout(env) for env in config.environments]}
    )
