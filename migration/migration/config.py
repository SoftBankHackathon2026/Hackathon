"""YAML 설정과 환경변수를 읽어 접속 정보를 조립한다. 비밀번호는 로그·repr에 노출하지 않는다."""

from __future__ import annotations

import os
import re
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
class BotConfig:
    """HTTP 요청 생성기. DB 대상 변경은 ingress 가 담당한다."""

    control_url: str = ""
    token_env: str = "BOT_CONTROL_TOKEN"
    request_endpoint: str = ""
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit
        import math
        for name in ("control_url", "request_endpoint"):
            value = getattr(self, name)
            if not value:
                continue
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ConfigError(f"bot.{name}: 인증정보·쿼리 없는 HTTP(S) URL 필요")
        if not self.token_env or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ConfigError("bot: token_env 와 양수 timeout_seconds 필요")


@dataclass(frozen=True)
class CutoverConfig:
    demo_total_rows: int | None = None  # opt-in: 800 seed + 100 CDC + 100 target
    state_dir: str = "/var/lib/worker"
    drop_source_slot_on_complete: bool = True   # COMPLETED 직전에 원본 slot·publication 제거 (WAL 축적 방지)
    min_live_rows_after_snapshot: int = 100
    min_rows_after_switch: int = 100
    offset_stable_seconds: float = 10.0
    poll_interval_seconds: float = 5.0
    stage_timeout_seconds: float = 1800.0
    # 기준점(COPYING 시작 때 원본 max(created_at)) 이전 집합을 CATCHING_UP 에서 한 번 어떻게 확정할지.
    # digest: 서버측 버킷 해시(행 전송 없음), rows: 행 전체 읽기. 전환 구간(DRAINING 이후)은 행을 읽지 않고 건수만 센다.
    snapshot_compare: str = "digest"

    def __post_init__(self) -> None:
        if self.demo_total_rows is not None and (
            type(self.demo_total_rows) is not int or self.demo_total_rows != 1000
            or self.min_live_rows_after_snapshot != 100 or self.min_rows_after_switch != 100
        ):
            raise ConfigError("demo_total_rows requires 1000 total and 100/100 live rows")
        if self.snapshot_compare not in ("digest", "rows"):
            raise ConfigError(f"cutover.snapshot_compare 는 digest|rows 중 하나다: {self.snapshot_compare}")


@dataclass(frozen=True)
class AppCheckConfig:
    """접수 큐를 어떤 대상으로 돌리기 전에 그 대상 앱이 준비됐는지 확인하는 기준."""

    url: str
    environment: str | None = None   # /api/info 의 environment 기대값 (예: aws)
    database: str | None = None      # /api/info 의 database.name 기대값 (예: migration_demo)
    token_env: str | None = None     # Bearer 토큰 환경변수 (HTTPS 대상에만)


@dataclass(frozen=True)
class IngressConfig:
    """접수 서비스(durable-ingress) 제어. control_url 이 없으면 cutover 는 큐를 건드리지 않는다(수동 운영)."""

    control_url: str | None = None          # 예: http://durable-ingress-control:9092
    token_env: str | None = None            # 제어 API Bearer 토큰 환경변수 (선택)
    timeout_seconds: float = 30.0
    # job 의 target 이름 → 접수 서비스 INGRESS_TARGETS 의 이름. 예: {onprem: onprem, aws: aws_db}
    targets: Mapping[str, str] = field(default_factory=dict)
    # job 의 target 이름 → 큐를 돌리기 전 확인할 앱. 없으면 앱 확인 없이 큐만 돌린다.
    app_checks: Mapping[str, AppCheckConfig] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.control_url)

    def ingress_target(self, job_target: str) -> str:
        try:
            return self.targets[job_target]
        except KeyError as exc:
            raise ConfigError(f"ingress.targets 에 {job_target} 이 없다 (있는 것: {', '.join(self.targets) or '없음'})") from exc


@dataclass(frozen=True)
class TrafficConfig:
    app: str | None = None
    platform_url_env: str = "ONEPORT_PLATFORM_URL"
    token_env: str = "ONEPORT_INGEST_TOKEN"
    timeout_seconds: float = 15.0
    targets: Mapping[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.app)

    def route_target(self, job_target: str) -> str:
        try:
            return self.targets[job_target]
        except KeyError as exc:
            raise ConfigError(f"traffic.targets 에 {job_target} 이 없다 (있는 것: {', '.join(self.targets) or '없음'})") from exc


@dataclass(frozen=True)
class Config:
    """설정 파일 전체."""

    targets: Mapping[str, TargetConfig]
    job: JobConfig = field(default_factory=JobConfig)
    kafka: KafkaConfig = field(default_factory=KafkaConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)  # standalone legacy CLI only
    bot: BotConfig = field(default_factory=BotConfig)
    cutover: CutoverConfig = field(default_factory=CutoverConfig)
    ingress: IngressConfig = field(default_factory=IngressConfig)
    traffic: TrafficConfig = field(default_factory=TrafficConfig)

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
        elif ftype in ("int | None", int | None):
            if value is not None and type(value) is not int:
                raise ConfigError(f"{section}.{key}: 정수 또는 null 이어야 한다")
            kwargs[key] = value
        elif ftype in ("int", int):
            kwargs[key] = _as_int(value, f"{section}.{key}")
        elif ftype in ("float", float):
            kwargs[key] = float(value)
        else:
            kwargs[key] = str(value)
    return cls(**kwargs)


def _parse_ingress(raw: Mapping[str, Any] | None, targets: Mapping[str, TargetConfig]) -> IngressConfig:
    raw = raw or {}
    allowed = {"control_url", "token_env", "timeout_seconds", "targets", "app_checks"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"ingress: 알 수 없는 키 {sorted(unknown)}")
    control_url = raw.get("control_url")
    control_url = str(control_url) if control_url else None
    mapping_raw = raw.get("targets") or {}
    if not isinstance(mapping_raw, Mapping):
        raise ConfigError("ingress.targets 는 {job target: 접수 서비스 target} 매핑이어야 한다")
    mapping = {str(k): str(v) for k, v in mapping_raw.items()}
    for name in mapping:
        if name not in targets:
            raise ConfigError(f"ingress.targets 의 {name} 은 targets 에 없다")
    checks_raw = raw.get("app_checks") or {}
    if not isinstance(checks_raw, Mapping):
        raise ConfigError("ingress.app_checks 는 {job target: {url, environment, database, token_env}} 매핑이어야 한다")
    checks: dict[str, AppCheckConfig] = {}
    for name, entry in checks_raw.items():
        if str(name) not in targets:
            raise ConfigError(f"ingress.app_checks 의 {name} 은 targets 에 없다")
        if not isinstance(entry, Mapping) or not entry.get("url"):
            raise ConfigError(f"ingress.app_checks.{name}.url 이 비어 있다")
        extra = set(entry) - {"url", "environment", "database", "token_env"}
        if extra:
            raise ConfigError(f"ingress.app_checks.{name}: 알 수 없는 키 {sorted(extra)}")
        checks[str(name)] = AppCheckConfig(url=str(entry["url"]),
                                           environment=str(entry["environment"]) if entry.get("environment") else None,
                                           database=str(entry["database"]) if entry.get("database") else None,
                                           token_env=str(entry["token_env"]) if entry.get("token_env") else None)
    if control_url and not mapping:
        raise ConfigError("ingress.control_url 이 있으면 ingress.targets 매핑이 필요하다")
    return IngressConfig(control_url=control_url, token_env=str(raw["token_env"]) if raw.get("token_env") else None,
                         timeout_seconds=float(raw.get("timeout_seconds", 30.0)), targets=mapping, app_checks=checks)


_ROUTE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _parse_traffic(raw: Mapping[str, Any] | None, targets: Mapping[str, TargetConfig]) -> TrafficConfig:
    raw = raw or {}
    allowed = {"app", "platform_url_env", "token_env", "timeout_seconds", "targets"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"traffic: 알 수 없는 키 {sorted(unknown)}")
    app = str(raw["app"]) if raw.get("app") else None
    mapping_raw = raw.get("targets") or {}
    if not isinstance(mapping_raw, Mapping):
        raise ConfigError("traffic.targets 는 {job target: 플랫폼 서버 경로 이름} 매핑이어야 한다")
    mapping = {str(k): str(v) for k, v in mapping_raw.items()}
    for name, route in mapping.items():
        if name not in targets:
            raise ConfigError(f"traffic.targets 의 {name} 은 targets 에 없다")
        if not _ROUTE_NAME.fullmatch(route):
            raise ConfigError(f"traffic.targets.{name} 은 소문자·숫자·- 로 된 이름이어야 한다: {route}")
    if app is not None and not _ROUTE_NAME.fullmatch(app):
        raise ConfigError(f"traffic.app 은 소문자·숫자·- 로 된 이름이어야 한다: {app}")
    if app and not mapping:
        raise ConfigError("traffic.app 이 있으면 traffic.targets 매핑이 필요하다")
    kwargs: dict[str, Any] = {"app": app, "targets": mapping,
                              "timeout_seconds": float(raw.get("timeout_seconds", 15.0))}
    for key in ("platform_url_env", "token_env"):
        if raw.get(key):
            kwargs[key] = str(raw[key])
    return TrafficConfig(**kwargs)


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
        bot=_dataclass_from(BotConfig, raw.get("bot"), "bot"),
        cutover=_dataclass_from(CutoverConfig, raw.get("cutover"), "cutover"),
        ingress=_parse_ingress(raw.get("ingress"), targets),
        traffic=_parse_traffic(raw.get("traffic"), targets),
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
