import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

from controlplane.domain.models import DeploymentRequest, Runtime
from controlplane.execution.catalog_worker import CatalogAwsWorker
from controlplane.repositories.releases import ReleaseStore
from oneport_aws.execution.oneport_ecs_app_model import RegisteredApp
from oneport_aws.execution.oneport_ecs_apps import AppRegistry
from oneport_aws.execution.oneport_ecs_db import provision_step
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsRuntime, OneportEcsSettings
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.provisioning import ResourceState
from oneport_aws.providers.oneport_ecs.registry import EcrImageBuilder

DB_SETTLED = frozenset(
    {ResourceState.READY, ResourceState.FAILED, ResourceState.UNCERTAIN, ResourceState.NONE}
)
RELEASE_DONE = frozenset({"succeeded", "failed"})
WAIT_POLL_SECONDS = 15


class UsageError(Exception):
    pass


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str, indent=2))


def log_stderr(line: str) -> None:
    print(line, file=sys.stderr)


@dataclass
class OperatorSession:
    args: argparse.Namespace
    settings: OneportEcsSettings
    runtime: OneportEcsRuntime | None = None
    worker: CatalogAwsWorker | None = None
    store: ReleaseStore | None = None
    registry: AppRegistry | None = None

    @property
    def app_id(self) -> str:
        return self.settings.application

    @property
    def env_id(self) -> str:
        return self.settings.environment_id

    def connected(self) -> OneportEcsRuntime:
        if self.runtime is None:
            self.runtime = OneportEcsRuntime(self.settings)
            self.runtime.connect()
        return self.runtime

    def provider(self) -> OneportEcsProvider:
        return self.connected().provider

    def open_journal(self) -> None:
        self.store = ReleaseStore(self.settings.database_url.get_secret_value())
        self.store.migrate()
        self.registry = AppRegistry(self.store.dsn)
        self.registry.migrate()

    def owns(self, row: dict | None) -> bool:
        return (
            row is not None
            and row["runtime"] == Runtime.AWS_ECS
            and (row["application"], row["environment_id"]) == (self.app_id, self.env_id)
        )

    def has_active_release(self) -> bool:
        return any(self.owns(row) and row["state"] not in RELEASE_DONE for row in self.store.list())

    def require_flag(self, value: Any, message: str) -> None:
        if not value:
            raise UsageError(message)

    def close(self) -> None:
        if self.worker:
            self.worker.close()
        if self.runtime:
            self.runtime.close()


def build_image(session: OperatorSession, request: DeploymentRequest) -> DeploymentRequest:
    provider = session.provider()
    builder = EcrImageBuilder(provider.aws.ecr, provider.environment)
    return builder.build(request, log=log_stderr)


def wait_for_db(session: OperatorSession) -> RegisteredApp:
    provider = session.provider()
    while True:
        result = provision_step(
            session.registry, provider, settle_seconds=session.settings.db_settle_seconds
        )
        app = session.registry.get(session.app_id, session.env_id)
        if result:
            log_stderr(f"db {result.state}: {result.message}")
        if app.db_state in DB_SETTLED:
            return app
        time.sleep(WAIT_POLL_SECONDS)


def get_registered_app(session: OperatorSession) -> RegisteredApp:
    app = session.registry.get(session.app_id, session.env_id)
    if app is None:
        raise ValueError("등록되지 않은 앱입니다. register를 먼저 실행하세요.")
    return app
