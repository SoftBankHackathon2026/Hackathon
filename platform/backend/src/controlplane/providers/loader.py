from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from controlplane.core.config import PlatformEnvironment
from controlplane.core.secrets import DatabaseSettings, secret_from_environment
from controlplane.domain.models import Runtime
from controlplane.providers.factory import adapter_from_file
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.releases import ReleaseStore


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Runtime
    config_file: Path
    listed: bool = True


class PlatformConfig(DatabaseSettings):
    providers: list[ProviderConfig] = Field(min_length=1)


@dataclass(frozen=True)
class ProviderEntry:
    runtime: Runtime
    path: Path
    listed: bool


@dataclass(frozen=True)
class LoadedPlatform:
    registry: ProviderRegistry
    store: ReleaseStore


class InvalidPlatformConfig(ValueError):
    pass


def _bundle_entries(bundle_path: Path) -> tuple[list[ProviderEntry], str]:
    bundle_file = bundle_path.resolve()
    bundle = PlatformConfig.model_validate_json(bundle_file.read_text())
    entries = [
        ProviderEntry(
            entry.runtime, (bundle_file.parent / entry.config_file).resolve(), entry.listed
        )
        for entry in bundle.providers
    ]
    return entries, bundle.database_url.get_secret_value()


def _platform_entries(environment: PlatformEnvironment) -> tuple[list[ProviderEntry], str | None]:
    if environment.config and environment.runtime_config:
        raise InvalidPlatformConfig("Use either ONEPORT_CONFIG or ONEPORT_RUNTIME_CONFIG")
    if environment.config:
        return _bundle_entries(environment.config)
    legacy = ProviderEntry(Runtime.ONPREM_ARGO, Path(environment.runtime_config).resolve(), True)
    return [legacy], None


def _register(registry: ProviderRegistry, entries: list[ProviderEntry], dsn: str | None) -> str:
    for entry in entries:
        adapter = adapter_from_file(entry.runtime, entry.path)
        adapter_dsn = adapter.settings.database_url.get_secret_value()
        if dsn is not None and adapter_dsn != dsn:
            raise InvalidPlatformConfig("API and workers must use the same database connection")
        dsn = adapter_dsn
        registry.register(adapter, listed=entry.listed)
    if dsn is None:
        raise InvalidPlatformConfig("No provider configured")
    return dsn


def _dashboard_only() -> LoadedPlatform | None:
    """배포 Provider 없이 대시보드(실측 저널·로그인)용 DB만 쓴다.

    ONEPORT_DATABASE_URL 또는 ONEPORT_DATABASE_URL_FILE 로 지정한다. 둘 다 없으면 시뮬레이션이다.
    """
    dsn = secret_from_environment("ONEPORT_DATABASE_URL").get_secret_value()
    if not dsn:
        return None
    registry = ProviderRegistry()
    store = ReleaseStore(dsn)
    registry.enable_catalog(store)
    return LoadedPlatform(registry, store)


def load_platform(environment: PlatformEnvironment | None = None) -> LoadedPlatform | None:
    environment = environment or PlatformEnvironment()
    if not environment.config and not environment.runtime_config:
        return _dashboard_only()
    registry = ProviderRegistry()
    try:
        entries, dsn = _platform_entries(environment)
        dsn = _register(registry, entries, dsn)
    except InvalidPlatformConfig:
        raise
    except (OSError, ValueError, KeyError, TypeError, ImportError):
        raise InvalidPlatformConfig(
            "Invalid platform/provider configuration; check files and installed extras"
        ) from None
    store = ReleaseStore(dsn)
    registry.enable_catalog(store)
    return LoadedPlatform(registry, store)
