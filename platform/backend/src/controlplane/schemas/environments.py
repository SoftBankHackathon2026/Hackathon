from typing import Literal

from pydantic import Field

from controlplane.domain.models import Model
from controlplane.schemas.common import IdempotencyKey


class ApplicationRegistration(Model):
    port: int = Field(default=8080, ge=1, le=65535)
    health_path: str = Field(default="/health", pattern=r"^/[A-Za-z0-9/_-]*$")
    database: Literal["none", "postgresql"] = "none"
    database_mode: Literal["shared", "dedicated"] = "shared"
    redis: bool = False


class AppDeletionRequest(Model):
    confirm: str = Field(min_length=1, max_length=64)
    delete_data: bool = False
    idempotency_key: IdempotencyKey
