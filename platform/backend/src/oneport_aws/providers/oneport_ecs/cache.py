"""Per-app cache (infra data-access.md §4 "캐시 생성 (필요한 앱만)").

ElastiCache Serverless (Valkey) `oneport-cache-<app-id>` in the cache subnets with
the data SG, TLS only. The cache, its user group and the app user share one name;
a disabled `default` user (`<name>-d`) is required in every user group. Requested
when the user chooses a cache at app registration and created in the background,
one bounded step per call, independent of a release deadline.

    requested -> creating -> configuring -> ready      (failed on terminal states)

Password handling mirrors database.py: users are created with throwaway
passwords. Once the cache is available, a new password is set on the app user
and, in the same step, written as REDIS_URL to `oneport/<app-id>/cache`. The
durable caller commits a write intent before each mutation; an interrupted write
is stopped for operator reconciliation, never automatically repeated.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from oneport_aws.providers.oneport_ecs.gateways.elasticache import ServerlessCache
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.provisioning import (
    ALPHANUMERIC,
    NO_STEP_MESSAGE,
    ResourceState,
    Step,
    random_password,
)
from oneport_aws.providers.oneport_ecs.secrets import find_writable_secret, put_secret
from oneport_aws.providers.oneport_ecs.task_payload import CACHE_BINDING

NOT_CONFIGURED = "AWS_CACHE_SUBNET_IDS / AWS_CACHE_SECURITY_GROUP_ID가 .env에 없습니다."
VANISHED = "캐시가 보이지 않아 다시 요청합니다."


class CacheStatus(StrEnum):
    AVAILABLE = "available"
    DELETING = "deleting"
    CREATE_FAILED = "create-failed"


class EntityStatus(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"


@dataclass(frozen=True)
class _Context:
    provider: OneportEcsProvider
    cache: ServerlessCache | None
    age_seconds: float
    settle_seconds: float
    before_write: Callable[[ResourceState], None]

    @property
    def status(self) -> str | None:
        return self.cache.status if self.cache else None


def _tags(provider: OneportEcsProvider) -> list[dict[str, str]]:
    env = provider.environment
    return [{"Key": env.runtime_app_tag_key, "Value": env.application}]


def describe(provider: OneportEcsProvider) -> ServerlessCache | None:
    return provider.gateways.elasticache.find_cache(provider.environment.cache_name)


def _create_users_and_group(provider: OneportEcsProvider) -> None:
    env, ec = provider.environment, provider.gateways.elasticache
    name = env.cache_name
    users = (
        (name, env.application, "on ~* +@all"),
        (f"{name}-d", "default", "off -@all"),
    )
    for user_id, user_name, access in users:
        if ec.find_user(user_id) is None:
            ec.create_user(
                user_id=user_id,
                user_name=user_name,
                access=access,
                password=random_password(ALPHANUMERIC),
                tags=_tags(provider),
            )
    if ec.find_user_group(name) is None:
        ec.create_user_group(name, [name, f"{name}-d"], _tags(provider))


def advance(
    provider: OneportEcsProvider,
    state: str,
    *,
    age_seconds: float = 0,
    settle_seconds: float = 30,
    before_write: Callable[[ResourceState], None],
) -> Step:
    env = provider.environment
    if not env.cache_subnets or not env.cache_security_group:
        return Step(ResourceState.FAILED, NOT_CONFIGURED)
    context = _Context(provider, describe(provider), age_seconds, settle_seconds, before_write)
    if context.status == CacheStatus.CREATE_FAILED:
        return Step(ResourceState.FAILED, f"캐시 상태가 {context.status}입니다.")
    if context.status == CacheStatus.DELETING:
        return Step(ResourceState.FAILED, "캐시가 삭제 중입니다.")
    handlers = {
        ResourceState.REQUESTED: _request,
        ResourceState.CREATING: _create_or_reset,
        ResourceState.CONFIGURING: _settle,
    }
    handler = handlers.get(ResourceState(state))
    if handler is None:
        return Step(ResourceState(state), NO_STEP_MESSAGE)
    return handler(context)


def _request(context: _Context) -> Step:
    provider, env = context.provider, context.provider.environment
    ec = provider.gateways.elasticache
    if context.cache is not None:
        return Step(ResourceState.CREATING, f"{env.cache_name}가 이미 있어 이어서 준비합니다.")
    if ec.find_user_group(env.cache_name) is not None and ec.find_user(env.cache_name) is not None:
        return Step(
            ResourceState.CREATING, f"{env.cache_name} user group이 있어 이어서 준비합니다."
        )
    find_writable_secret(provider, CACHE_BINDING)
    context.before_write(ResourceState.PROVISIONING)
    _create_users_and_group(provider)
    return Step(ResourceState.CREATING, f"{env.cache_name} 사용자·user group 생성을 요청했습니다.")


def _create_or_reset(context: _Context) -> Step:
    if context.cache is None:
        return _create_cache(context)
    return _reset_credentials(context)


def _create_cache(context: _Context) -> Step:
    provider, env = context.provider, context.provider.environment
    group = provider.gateways.elasticache.find_user_group(env.cache_name)
    if group is None:
        return Step(ResourceState.REQUESTED, "user group이 보이지 않아 다시 요청합니다.")
    if group.status != EntityStatus.ACTIVE:
        return Step(ResourceState.CREATING, f"user group: {group.status}")
    context.before_write(ResourceState.PROVISIONING)
    provider.gateways.elasticache.create_cache(
        name=env.cache_name,
        subnet_ids=env.cache_subnets,
        security_group=env.cache_security_group,
        max_storage_gb=env.cache_max_storage_gb,
        max_ecpu=env.cache_max_ecpu,
        tags=_tags(provider),
    )
    return Step(ResourceState.CREATING, f"{env.cache_name} 생성을 요청했습니다 (약 1~2분).")


def _reset_credentials(context: _Context) -> Step:
    provider, env, cache = context.provider, context.provider.environment, context.cache
    if context.status != CacheStatus.AVAILABLE or cache is None or cache.endpoint is None:
        return Step(ResourceState.CREATING, f"{env.cache_name}: {context.status}")
    find_writable_secret(provider, CACHE_BINDING)
    context.before_write(ResourceState.RESETTING)
    password = random_password(ALPHANUMERIC)
    provider.gateways.elasticache.set_user_password(env.cache_name, password)
    endpoint = cache.endpoint
    url = f"rediss://{quote(env.application)}:{quote(password)}@{endpoint.address}:{endpoint.port}"
    ref = put_secret(provider, CACHE_BINDING, url)
    return Step(
        ResourceState.CONFIGURING,
        f"비밀번호를 설정하고 REDIS_URL을 {env.secret_name(CACHE_BINDING)}에 저장했습니다.",
        ref=ref,
    )


def _settle(context: _Context) -> Step:
    provider, env = context.provider, context.provider.environment
    if context.cache is None:
        return Step(ResourceState.REQUESTED, VANISHED)
    user = provider.gateways.elasticache.find_user(env.cache_name)
    settled = (
        context.status == CacheStatus.AVAILABLE
        and user is not None
        and user.status == EntityStatus.ACTIVE
        and context.age_seconds >= context.settle_seconds
    )
    if settled:
        return Step(ResourceState.READY, f"{env.cache_name} 준비 완료.")
    return Step(ResourceState.CONFIGURING, f"{env.cache_name}: {context.status}")


def delete_step(provider: OneportEcsProvider) -> tuple[bool, str]:
    env, ec = provider.environment, provider.gateways.elasticache
    name = env.cache_name
    cache = describe(provider)
    if cache is not None:
        if cache.status != CacheStatus.DELETING:
            ec.delete_cache(name)
        return False, f"{name} 캐시 삭제 중 (약 4분)"
    group = ec.find_user_group(name)
    if group is not None:
        if group.status != EntityStatus.DELETING:
            ec.delete_user_group(name)
        return False, f"{name} user group 삭제 중"
    user_ids = (name, f"{name}-d")
    for user_id in user_ids:
        user = ec.find_user(user_id)
        if user is not None and user.status != EntityStatus.DELETING:
            ec.delete_user(user_id)
    if any(ec.find_user(user_id) is not None for user_id in user_ids):
        return False, "캐시 사용자 삭제 중"
    return True, f"{name} 캐시·user group·사용자를 삭제했습니다."
