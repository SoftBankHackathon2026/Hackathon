from pydantic import Field

from controlplane.domain.models import Model


class CreateApplication(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,29}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{16,128}$")


class AttachEnvironment(Model):
    target_id: str = Field(min_length=1, max_length=80)
