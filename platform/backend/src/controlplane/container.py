from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import SecretStr

from controlplane.builds.models import BuildSettings
from controlplane.builds.store import BuildStore
from controlplane.core.config import (
    MIN_OPERATOR_TOKEN_LENGTH,
    AuthMode,
    HttpSettings,
    deployment_auth_mode,
    ingest_token_from_environment,
    operator_token_from_environment,
)
from controlplane.cost.config import load_cost_config
from controlplane.cost.service import CostService
from controlplane.cost.store import CostStore
from controlplane.demo_deployments.models import RunSettings
from controlplane.demo_deployments.store import RunStore
from controlplane.domain.fleet import FleetConfig
from controlplane.integrations.base import FleetSource
from controlplane.integrations.config import load_fleet_config
from controlplane.integrations.live import JournalFleet
from controlplane.integrations.simulator import SimulatedFleet
from controlplane.providers.loader import load_platform
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories.cutover_commands import (
    CommandRepository,
    CutoverCommandJournal,
    MemoryCutoverCommands,
)
from controlplane.repositories.enrollments import EnrollmentStore
from controlplane.repositories.fleet_journal import FleetJournal
from controlplane.repositories.releases import ReleaseStore
from controlplane.services.auth import AuthService, LoginThrottle
from controlplane.services.builds import BuildService
from controlplane.services.cutover_commands import CutoverCommandService
from controlplane.services.deployments import DeploymentService
from controlplane.services.enrollments import EnrollmentService, aws_principal_from_environment
from controlplane.services.environments import EnvironmentService
from controlplane.services.fleet import FleetService
from controlplane.services.ingest import IngestService
from controlplane.services.migrations import MigrationService
from controlplane.services.monitoring import MonitoringService
from controlplane.services.runs import DemoRunService
from controlplane.services.security import SecurityService

if TYPE_CHECKING:
    from migration_connection.control.service import ConnectionService


@dataclass(frozen=True)
class Container:
    auth_mode: AuthMode
    http: HttpSettings
    registry: ProviderRegistry
    store: ReleaseStore | None
    operator_token: SecretStr | None
    build_settings: BuildSettings | None
    run_settings: RunSettings | None
    deployments: DeploymentService
    environments: EnvironmentService
    monitoring: MonitoringService
    builds: BuildService
    runs: DemoRunService
    auth: AuthService | None
    migrations: MigrationService
    fleet: FleetService
    security: SecurityService
    ingest: IngestService | None
    ingest_token: SecretStr | None
    costs: CostService
    cutover_commands: CutoverCommandService
    enrollments: EnrollmentService | None = None
    connections: ConnectionService | None = None

    @property
    def execution_configured(self) -> bool:
        return self.store is not None

    def prepare(self) -> None:
        if self.store is None:
            return
        self.store.migrate()
        self.registry.initialize()
        if self.build_settings:
            BuildStore(self.store.dsn).migrate()
        if self.run_settings:
            RunStore(self.store.dsn).migrate()


def _resolve_platform(
    registry: ProviderRegistry | None,
    store: ReleaseStore | None,
    operator_token: str | None,
    auth_mode: AuthMode,
) -> tuple[ProviderRegistry, ReleaseStore | None, SecretStr | None]:
    token = SecretStr(operator_token) if operator_token else None
    if registry is not None:
        return registry, store, token
    loaded = load_platform()
    if loaded is None:
        return ProviderRegistry(), None, None
    if auth_mode == AuthMode.OPERATOR and loaded.registry.runtimes():
        token = operator_token_from_environment()
    return loaded.registry, loaded.store, token


def _check_operator_token(
    auth_mode: AuthMode,
    store: ReleaseStore | None,
    token: SecretStr | None,
    registry: ProviderRegistry | None = None,
) -> None:
    # 배포 Provider 가 없는 대시보드 전용 구성은 실행할 배포가 없으므로 운영자 토큰을 요구하지 않는다.
    if auth_mode != AuthMode.OPERATOR or store is None:
        return
    if registry is not None and not registry.runtimes():
        return
    if not token or len(token.get_secret_value()) < MIN_OPERATOR_TOKEN_LENGTH:
        raise ValueError("Execution requires an explicit operator token")


def _command_store(store: ReleaseStore | None) -> CommandRepository:
    return CutoverCommandJournal(store) if store is not None else MemoryCutoverCommands()


def _fleet_source(config: FleetConfig, store: ReleaseStore | None) -> FleetSource:
    if config.mode == "live" and store is not None:
        return JournalFleet(config, FleetJournal(store))
    return SimulatedFleet(config)


def _connections(store: ReleaseStore | None, mode: AuthMode) -> ConnectionService | None:
    # Optional package: unchanged legacy deployments do not require it.
    if mode not in {AuthMode.DEMO, AuthMode.LOGIN}:
        return None
    try:
        from migration_connection.platform_bridge.factory import build
    except ImportError:
        return None
    return build(store)


def build_container(
    *,
    registry: ProviderRegistry | None = None,
    store: ReleaseStore | None = None,
    operator_token: str | None = None,
    http_settings: HttpSettings | None = None,
    auth_mode: AuthMode | str | None = None,
) -> Container:
    mode = AuthMode(auth_mode) if auth_mode is not None else deployment_auth_mode()
    registry, store, token = _resolve_platform(registry, store, operator_token, mode)
    _check_operator_token(mode, store, token, registry)
    if store:
        registry.enable_catalog(store)
    build_settings = BuildSettings.from_environment()
    run_settings = RunSettings.from_environment()
    deployments = DeploymentService(registry, store)
    fleet_config = load_fleet_config()
    source = _fleet_source(fleet_config, store)
    migrations = MigrationService(source)
    return Container(
        auth_mode=mode,
        http=http_settings or HttpSettings.from_environment(),
        registry=registry,
        store=store,
        operator_token=token,
        build_settings=build_settings,
        run_settings=run_settings,
        deployments=deployments,
        environments=EnvironmentService(deployments),
        monitoring=MonitoringService(deployments),
        builds=BuildService(build_settings, store, deployments),
        runs=DemoRunService(run_settings, build_settings, deployments),
        auth=AuthService(deployments.accounts, LoginThrottle()) if deployments.accounts else None,
        migrations=migrations,
        fleet=FleetService(source, migrations),
        security=SecurityService(source),
        ingest=IngestService(fleet_config, FleetJournal(store)) if store else None,
        ingest_token=ingest_token_from_environment(),
        costs=CostService(
            fleet_config,
            load_cost_config(),
            CostStore(store).get if store else (lambda env_id: None),
        ),
        connections=_connections(store, mode),
        cutover_commands=CutoverCommandService(fleet_config, _command_store(store)),
        enrollments=EnrollmentService(EnrollmentStore(store), aws_principal_from_environment())
        if store
        else None,
    )
