import secrets
import string
from dataclasses import dataclass
from enum import StrEnum

from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment


class ResourceState(StrEnum):
    NONE = "none"
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    CREATING = "creating"
    RESETTING = "resetting"
    CONFIGURING = "configuring"
    READY = "ready"
    FAILED = "failed"
    UNCERTAIN = "uncertain"

    @property
    def in_progress(self) -> bool:
        return self in IN_PROGRESS


IN_PROGRESS = frozenset(
    {
        ResourceState.REQUESTED,
        ResourceState.PROVISIONING,
        ResourceState.CREATING,
        ResourceState.RESETTING,
        ResourceState.CONFIGURING,
    }
)
INTERRUPTED_WRITE = frozenset({ResourceState.PROVISIONING, ResourceState.RESETTING})
TERMINAL = frozenset({ResourceState.FAILED, ResourceState.UNCERTAIN})

ALPHANUMERIC = string.ascii_letters + string.digits
PASSWORD_LENGTH = 32
NO_STEP_MESSAGE = "진행할 단계가 없습니다."


@dataclass
class Step:
    state: ResourceState
    message: str
    ref: str | None = None


def random_password(alphabet: str) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(PASSWORD_LENGTH))


def app_tags(env: OneportEcsEnvironment) -> list[dict[str, str]]:
    return [
        {"Key": "App", "Value": env.application},
        {"Key": "launchpad:application", "Value": env.application},
        {"Key": "launchpad:environment", "Value": env.environment_id},
    ]
