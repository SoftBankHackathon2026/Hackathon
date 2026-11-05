"""Per-app PostgreSQL (infra Notion §3 step 3 "DB 생성 (필요한 앱만)").

RDS `oneport-db-<app-id>` in subnet group `oneport-data`, reachable only from ECS
tasks (data SG), never public. Requested when the user chooses a DB at app
registration; created in the background, one bounded step per call, so it is
not tied to a release's 12-minute deadline.

    requested -> creating -> configuring -> ready      (failed on terminal RDS states)

Password handling (oneport-deployer can write secrets but never read them):
the password used at creation is thrown away. When the instance is available,
a new password is generated and, in the same step, set on the instance
(ModifyDBInstance) and written as DATABASE_URL to `oneport/<app-id>/db`. The durable caller commits a write intent before each mutation. An interrupted
write is stopped for operator reconciliation, never automatically repeated.
"""

import string
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from oneport_aws.providers.oneport_ecs.gateways.rds import DbInstance
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.provisioning import (
    NO_STEP_MESSAGE,
    ResourceState,
    Step,
    app_tags,
    random_password,
)
from oneport_aws.providers.oneport_ecs.secrets import find_writable_secret, put_secret
from oneport_aws.providers.oneport_ecs.task_payload import DB_BINDING

USERNAME = "app_admin"
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_"
NOT_CONFIGURED = "AWS_RDS_SUBNET_GROUP / AWS_RDS_SECURITY_GROUP_ID가 .env에 없습니다."
VANISHED = "DB가 보이지 않아 다시 요청합니다."


class RdsStatus(StrEnum):
    AVAILABLE = "available"
    DELETING = "deleting"
    FAILED = "failed"
    INCOMPATIBLE_PARAMETERS = "incompatible-parameters"
    INCOMPATIBLE_NETWORK = "incompatible-network"
    INCOMPATIBLE_RESTORE = "incompatible-restore"
    STORAGE_FULL = "storage-full"
    INACCESSIBLE_ENCRYPTION_CREDENTIALS = "inaccessible-encryption-credentials"


FAILED_STATUSES = frozenset(set(RdsStatus) - {RdsStatus.AVAILABLE, RdsStatus.DELETING})


@dataclass(frozen=True)
class _Context:
    provider: OneportEcsProvider
    instance: DbInstance | None
    age_seconds: float
    settle_seconds: float
    instance_class: str
    storage_gib: int
    before_write: Callable[[ResourceState], None]

    @property
    def status(self) -> str | None:
        return self.instance.status if self.instance else None


def database_name(application: str) -> str:
    return application.replace("-", "_")[:63]


def describe(provider: OneportEcsProvider) -> DbInstance | None:
    return provider.gateways.rds.find_instance(provider.environment.db_instance)


def advance(
    provider: OneportEcsProvider,
    state: str,
    *,
    age_seconds: float = 0,
    settle_seconds: float = 60,
    instance_class: str = "db.t4g.micro",
    storage_gib: int = 20,
    before_write: Callable[[ResourceState], None],
) -> Step:
    env = provider.environment
    if not env.rds_subnet_group or not env.rds_security_group:
        return Step(ResourceState.FAILED, NOT_CONFIGURED)
    context = _Context(
        provider=provider,
        instance=describe(provider),
        age_seconds=age_seconds,
        settle_seconds=settle_seconds,
        instance_class=instance_class,
        storage_gib=storage_gib,
        before_write=before_write,
    )
    if context.status in FAILED_STATUSES:
        return Step(ResourceState.FAILED, f"RDS 상태가 {context.status}입니다.")
    if context.status == RdsStatus.DELETING:
        return Step(ResourceState.FAILED, "DB가 삭제 중입니다.")
    handlers = {
        ResourceState.REQUESTED: _request,
        ResourceState.CREATING: _reset_credentials,
        ResourceState.CONFIGURING: _settle,
    }
    handler = handlers.get(ResourceState(state))
    if handler is None:
        return Step(ResourceState(state), NO_STEP_MESSAGE)
    return handler(context)


def _request(context: _Context) -> Step:
    provider, env = context.provider, context.provider.environment
    if context.instance is not None:
        return Step(ResourceState.CREATING, f"{env.db_instance}가 이미 있어 이어서 준비합니다.")
    find_writable_secret(provider, DB_BINDING)
    context.before_write(ResourceState.PROVISIONING)
    provider.gateways.rds.create_instance(
        identifier=env.db_instance,
        instance_class=context.instance_class,
        storage_gib=context.storage_gib,
        db_name=database_name(env.application),
        username=USERNAME,
        password=random_password(PASSWORD_ALPHABET),
        subnet_group=env.rds_subnet_group,
        security_group=env.rds_security_group,
        tags=app_tags(env),
    )
    return Step(ResourceState.CREATING, f"{env.db_instance} 생성을 요청했습니다 (약 5분).")


def _reset_credentials(context: _Context) -> Step:
    provider, env, instance = context.provider, context.provider.environment, context.instance
    if instance is None:
        return Step(ResourceState.REQUESTED, VANISHED)
    if context.status != RdsStatus.AVAILABLE or instance.endpoint is None:
        return Step(ResourceState.CREATING, f"{env.db_instance}: {context.status}")
    find_writable_secret(provider, DB_BINDING)
    context.before_write(ResourceState.RESETTING)
    password = random_password(PASSWORD_ALPHABET)
    provider.gateways.rds.set_master_password(env.db_instance, password)
    user = quote(instance.master_username or USERNAME)
    name = instance.db_name or database_name(env.application)
    endpoint = instance.endpoint
    url = (
        f"postgresql://{user}:{quote(password)}"
        f"@{endpoint.address}:{endpoint.port}/{name}?sslmode=require"
    )
    ref = put_secret(provider, DB_BINDING, url)
    return Step(
        ResourceState.CONFIGURING,
        f"비밀번호를 설정하고 DATABASE_URL을 {env.secret_name(DB_BINDING)}에 저장했습니다.",
        ref=ref,
    )


def _settle(context: _Context) -> Step:
    env = context.provider.environment
    if context.instance is None:
        return Step(ResourceState.REQUESTED, VANISHED)
    if context.status == RdsStatus.AVAILABLE and context.age_seconds >= context.settle_seconds:
        return Step(ResourceState.READY, f"{env.db_instance} 준비 완료.")
    return Step(ResourceState.CONFIGURING, f"{env.db_instance}: {context.status}")


def delete(provider: OneportEcsProvider, *, final_snapshot: bool = True, suffix: str = "") -> str:
    env = provider.environment
    snapshot = f"{env.db_instance}-final{suffix}" if final_snapshot else None
    provider.gateways.rds.delete_instance(env.db_instance, snapshot)
    return snapshot or "no snapshot"
