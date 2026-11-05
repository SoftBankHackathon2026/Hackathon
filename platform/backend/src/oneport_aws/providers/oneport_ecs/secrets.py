import re

from controlplane.providers.contracts import ProviderRejected
from oneport_aws.providers.oneport_ecs.gateways.secrets_manager import SecretInfo
from oneport_aws.providers.oneport_ecs.provider import OneportEcsProvider
from oneport_aws.providers.oneport_ecs.provisioning import app_tags

BINDING = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


def find_writable_secret(provider: OneportEcsProvider, binding: str) -> SecretInfo | None:
    found = provider.gateways.secrets.find_secret(provider.environment.secret_name(binding))
    if found and found.scheduled_for_deletion:
        raise ProviderRejected("Secret이 삭제 예약 상태입니다. 운영자가 복구 여부를 확인하세요.")
    return found


def put_secret(provider: OneportEcsProvider, binding: str, value: str) -> str:
    if not BINDING.fullmatch(binding):
        raise ValueError("binding은 소문자·숫자·하이픈 이름이어야 합니다.")
    if not value:
        raise ValueError("빈 값은 저장하지 않습니다.")
    env, secrets = provider.environment, provider.gateways.secrets
    found = find_writable_secret(provider, binding)
    if found:
        return secrets.put_value(found.arn, value).pinned_ref
    return secrets.create_secret(env.secret_name(binding), value, app_tags(env)).pinned_ref
