from datetime import datetime
from typing import Any

from pydantic import BaseModel


class BuildConfig(BaseModel):
    enabled: bool
    repositories: list[str]
    required_databases: dict[str, str]


class BuildLogs(BaseModel):
    lines: list[str]


class BuildView(BaseModel):
    id: str
    application: str
    environment_id: str
    state: str
    image: str | None
    release_id: str | None
    error_code: str | None
    events: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime
    repository: str
    revision: str
    commit: str | None
    build_id: str | None
