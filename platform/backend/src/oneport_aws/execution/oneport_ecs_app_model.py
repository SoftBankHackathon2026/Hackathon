from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from oneport_aws.providers.oneport_ecs.models import OneportEcsEnvironment
from oneport_aws.providers.oneport_ecs.provisioning import ResourceState

DatabaseChoice = Literal["none", "postgresql"]
DatabaseMode = Literal["shared", "dedicated"]


class ResourceKind(StrEnum):
    DB = "db"
    CACHE = "cache"


@dataclass(frozen=True)
class ResourceColumns:
    state: str
    error: str
    updated_at: str


RESOURCE_COLUMNS = {
    ResourceKind.DB: ResourceColumns("db_state", "db_error", "db_updated_at"),
    ResourceKind.CACHE: ResourceColumns("cache_state", "cache_error", "cache_updated_at"),
}


@dataclass(frozen=True)
class RegisteredApp:
    app_id: str
    environment_id: str
    port: int
    health_path: str
    domain: str
    rule_priority: int
    database: str
    database_mode: str
    db_state: ResourceState
    db_error: str | None
    redis: bool
    cache_state: ResourceState
    cache_error: str | None
    age_seconds: float = 0.0

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RegisteredApp":
        return cls(
            app_id=row["app_id"],
            environment_id=row["environment_id"],
            port=row["port"],
            health_path=row["health_path"],
            domain=row["domain"],
            rule_priority=row["rule_priority"],
            database=row["database"],
            database_mode=row["database_mode"],
            db_state=ResourceState(row["db_state"]),
            db_error=row["db_error"],
            redis=row["redis"],
            cache_state=ResourceState(row["cache_state"]),
            cache_error=row["cache_error"],
            age_seconds=float(row.get("age_seconds") or 0),
        )

    @property
    def has_dedicated_database(self) -> bool:
        return self.database == "postgresql" and self.database_mode == "dedicated"

    @property
    def wants_database(self) -> bool:
        return self.database == "postgresql"

    def state_of(self, kind: ResourceKind) -> ResourceState:
        return self.db_state if kind == ResourceKind.DB else self.cache_state


@dataclass(frozen=True)
class Registration:
    port: int
    health_path: str
    database: DatabaseChoice = "none"
    database_mode: DatabaseMode = "dedicated"
    shared_ref: str | None = None
    redis: bool | None = None

    def validate(self, env: OneportEcsEnvironment) -> None:
        if not self.health_path.startswith("/"):
            raise ValueError("health check 경로는 /로 시작해야 합니다.")
        if self.database not in {"none", "postgresql"}:
            raise ValueError("database는 none 또는 postgresql입니다.")
        if self.database_mode not in {"shared", "dedicated"}:
            raise ValueError("Unknown database mode")
        if self.wants_database and self.database_mode == "shared" and not self.shared_ref:
            raise ValueError("Shared DB의 앱 전용 Secret 참조를 먼저 등록하세요.")
        if env.required_database != "none" and self.database != env.required_database:
            raise ValueError("앱 계약에 PostgreSQL이 필수입니다. DB 없이 등록할 수 없습니다.")

    @property
    def wants_database(self) -> bool:
        return self.database == "postgresql"
