from pydantic import BaseModel

from controlplane.core.config import AuthMode
from controlplane.schemas.accounts import Identity


class Health(BaseModel):
    status: str
    mode: str
    execution_configured: bool


class Access(BaseModel):
    auth_mode: AuthMode
    execution_configured: bool
    user: Identity | None
