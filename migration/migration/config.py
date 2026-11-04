"""YAML 설정과 환경변수를 읽어 접속 정보를 조립한다. 비밀번호는 로그·repr에 노출하지 않는다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

ROLES: tuple[str, ...] = ("admin", "app", "cdc", "sink", "verify")
TARGET_KINDS = ("rds", "self_managed")

DEFAULT_PASSWORD_ENV: dict[str, str] = {
    "admin": "MIGRATION_ADMIN_PASSWORD",
    "app": "MIGRATION_APP_PASSWORD",
    "cdc": "MIGRATION_CDC_PASSWORD",
    "sink": "MIGRATION_SINK_PASSWORD",
    "verify": "MIGRATION_VERIFY_PASSWORD",
}

DEFAULT_USERS: dict[str, str] = {
    "admin": "postgres",
    "app": "migration_app",
    "cdc": "migration_cdc",
    "sink": "migration_sink",
    "verify": "migration_verify",
}


class ConfigError(ValueError):
    """설정 파일이나 환경변수가 잘못됐을 때."""


def migration_home() -> Path:
    """sql/·connectors/ 가 있는 저장소 루트. 컨테이너에서는 MIGRATION_HOME 으로 지정한다."""
    env = os.environ.get("MIGRATION_HOME")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class UserSpec:
    """한 역할의 로그인 사용자명과 비밀번호 환경변수 이름."""

    user: str
    password_env: str


@dataclass(frozen=True)
class TargetConfig:
    """DB 한 곳(onprem 또는 aws)의 접속 정보. 비밀번호는 포함하지 않는다."""

    name: str
    host: str
    port: int
    dbname: str
    sslmode: str = "require"
    admin_dbname: str = "postgres"
    users: Mapping[str, UserSpec] = field(default_factory=dict)
    # "rds" | "self_managed" | None. None 이면 init_db 가 job.source 여부로 추정한다 (source 가 아니면 RDS 취급).
    kind: str | None = None

    def user_spec(self, role: str) -> UserSpec:
        if role not in ROLES:
            raise ConfigError(f"알 수 없는 역할: {role}")
        spec = self.users.get(role)
        if spec is None:
            return UserSpec(DEFAULT_USERS[role], DEFAULT_PASSWORD_ENV[role])
        return spec


@dataclass(frozen=True)
class ConnectionSpec:
    """psycopg 접속에 필요한 전부. repr/str 에서 비밀번호를 가린다."""

    target: str
    role: str
    host: str
    port: int
    dbname: str
    user: str
    sslmode: str
    password: str = field(repr=False, compare=False)
    connect_timeout: int = 10
    application_name: str = "migration-tools"

    def conninfo(self) -> str:
        """비밀번호를 뺀 conninfo 문자열. 로그에 그대로 써도 된다."""
        return (
            f"host={self.host} port={self.port} dbname={self.dbname} user={self.user} "
            f"sslmode={self.sslmode} connect_timeout={self.connect_timeout} "
            f"application_name={self.application_name}"
        )

    def __str__(self) -> str:
        return f"{self.target}/{self.role} {self.user}@{self.host}:{self.port}/{self.dbname} (sslmode={self.sslmode})"


@dataclass(frozen=True)
class JobConfig:
    """전환 작업 하나의 식별자와 방향."""

    job_id: str = "forward_001"
    source: str = "onprem"
    target: str = "aws"
    table: str = "public.migration_demo"
    live_phase_before: str = "onprem_live"
    live_phase_after: str = "aws_live"

    @property
    def slot_name(self) -> str:
        return self.job_id

    @property
    def publication_name(self) -> str:
        return f"{self.job_id}_pub"

    @property
    def topic(self) -> str:
        return f"{self.job_id}.{self.table}"

    @property
    def source_connector(self) -> str:
        return f"{self.job_id}-source"

    @property
    def sink_connector(self) -> str:
        return f"{self.job_id}-sink"


@dataclass(frozen=True)
class KafkaConfig:
    bootstrap_servers: str = "kafka:9092"
    connect_url: str = "http://localhost:8083"
    secrets_file: str = "/opt/kafka/secrets/migration.properties"


@dataclass(frozen=True)
class WorkerConfig:
    url: str = "http://localhost:8080"
    state_dir: str = "/var/lib/worker"


@dataclass(frozen=True)
class CutoverConfig:
    state_dir: str = "/var/lib/worker"
    drop_source_slot_on_complete: bool = True   # COMPLETED 직전에 원본 slot·publication 제거 (WAL 축적 방지)
    min_live_rows_after_snapshot: int = 100
    min_rows_after_switch: int = 100
    offset_stable_seconds: float = 10.0
    poll_interval_seconds: float = 5.0
    stage_timeout_seconds: float = 1800.0


@dataclass(frozen=True)
class Config:
    """설정 파일 전체."""

    targets: Mapping[str, TargetConfig]
    job: JobConfig = field(default_factory=JobConfig)
    kafka: KafkaConfig = field(default_factory=KafkaConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    cutover: CutoverConfig = field(default_factory=CutoverConfig)

    def target(self, name: str) -> TargetConfig:
        try:
            return self.targets[name]
        except KeyError as exc:
            raise ConfigError(f"설정에 없는 target: {name} (있는 것: {', '.join(self.targets)})") from exc

    def connection(
        self,
        target_name: str,
        role: str,
        *,
        dbname: str | None = None,
        env: Mapping[str, str] | None = None,
        application_name: str = "migration-tools",
    ) -> ConnectionSpec:
        """target/role 조합의 접속 사양을 만든다. 비밀번호는 환경변수에서만 읽는다."""
        env = os.environ if env is None else env
        target = self.target(target_name)
        spec = target.user_spec(role)
        password = env.get(spec.password_env)
        if not password:
            raise ConfigError(f"환경변수 {spec.password_env} 가 비어 있다 (target={target_name}, role={role})")
        return ConnectionSpec(
            target=target_name,
            role=role,
            host=target.host,
            port=target.port,
            dbname=dbname or target.dbname,
            user=spec.user,
            sslmode=target.sslmode,
            password=password,
            application_name=application_name,
        )


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "1", "on"):
        return True
    if text in ("false", "no", "0", "off"):
        return False
    raise ConfigError(f"{name}: true/false 여야 한다 (받은 값: {value!r})")


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 은 정수여야 한다: {value!r}") from exc


def _parse_target(name: str, raw: Mapping[str, Any]) -> TargetConfig:
    if "host" not in raw:
        raise ConfigError(f"targets.{name}.host 가 없다")
    kind = raw.get("kind")
    if kind is not None and kind not in TARGET_KINDS:
        raise ConfigError(f"targets.{name}.kind: 허용되는 값은 {TARGET_KINDS} (받은 값: {kind!r})")
    users: dict[str, UserSpec] = {}
    for role, entry in (raw.get("users") or {}).items():
        if role not in ROLES:
            raise ConfigError(f"targets.{name}.users.{role}: 허용되는 역할은 {ROLES}")
        if not isinstance(entry, Mapping):
            raise ConfigError(f"targets.{name}.users.{role} 는 매핑이어야 한다")
        if "password" in entry:
            raise ConfigError(f"targets.{name}.users.{role}: 설정 파일에 password 를 적지 않는다. password_env 를 쓴다")
        users[role] = UserSpec(
            user=str(entry.get("user") or DEFAULT_USERS[role]),
            password_env=str(entry.get("password_env") or DEFAULT_PASSWORD_ENV[role]),
        )
    return TargetConfig(
        name=name,
        host=str(raw["host"]),
        port=_as_int(raw.get("port", 5432), f"targets.{name}.port"),
        dbname=str(raw.get("dbname", "migration_demo")),
        sslmode=str(raw.get("sslmode", "require")),
        admin_dbname=str(raw.get("admin_dbname", "postgres")),
        users=users,
        kind=None if kind is None else str(kind),
    )


def _dataclass_from(cls: type, raw: Mapping[str, Any] | None, section: str) -> Any:
    raw = raw or {}
    allowed = {f.name: f for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(raw) - set(allowed)
    if unknown:
        raise ConfigError(f"{section}: 알 수 없는 키 {sorted(unknown)}")
    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        ftype = allowed[key].type
        if ftype in ("bool", bool):
            kwargs[key] = _as_bool(value, f"{section}.{key}")
        elif ftype in ("int", int):
            kwargs[key] = _as_int(value, f"{section}.{key}")
        elif ftype in ("float", float):
            kwargs[key] = float(value)
        else:
            kwargs[key] = str(value)
    return cls(**kwargs)


def parse_config(raw: Mapping[str, Any]) -> Config:
    """YAML 로 읽은 매핑을 Config 로 변환한다."""
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, Mapping) or not targets_raw:
        raise ConfigError("targets 섹션이 비어 있다")
    targets = {name: _parse_target(str(name), entry or {}) for name, entry in targets_raw.items()}
    job = _dataclass_from(JobConfig, raw.get("job"), "job")
    if job.source not in targets or job.target not in targets:
        raise ConfigError(f"job.source/target 은 targets 에 있어야 한다: {job.source}, {job.target}")
    if job.source == job.target:
        raise ConfigError("job.source 와 job.target 이 같다")
    return Config(
        targets=targets,
        job=job,
        kafka=_dataclass_from(KafkaConfig, raw.get("kafka"), "kafka"),
        worker=_dataclass_from(WorkerConfig, raw.get("worker"), "worker"),
        cutover=_dataclass_from(CutoverConfig, raw.get("cutover"), "cutover"),
    )


def load_config(path: str | Path) -> Config:
    """YAML 파일을 읽어 Config 를 만든다."""
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일이 없다: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파일 파싱 실패: {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError("설정 파일 최상위는 매핑이어야 한다")
    return parse_config(raw)


def redact(text: str, secrets: Mapping[str, str] | None = None) -> str:
    """환경변수에 있는 비밀번호 값이 문자열에 섞여 있으면 가린다."""
    env = os.environ if secrets is None else secrets
    for key, value in env.items():
        if key.endswith("_PASSWORD") and value and value in text:
            text = text.replace(value, "***")
    return text
