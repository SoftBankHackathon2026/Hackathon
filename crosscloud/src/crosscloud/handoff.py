"""원가연 전환 도구에 넘길 인계 파일 — config.cloud.yaml 과 ingress.env.

config.cloud.yaml 형식의 정본은 migration/config.cloud.example.yaml 과
migration/migration/config.py·cloud/plan.py 다. targets 라벨은 클라우드
이름(aws·gcp)이고, 방향마다 새 job_id 를 쓴다(반대 방향은 새 job + 새 빈
대상 DB — CLOUD_MIGRATION.md).

비밀번호는 쓰지 않는다. 각 로그인에는 env 이름(password_env)만 적고, 운영자
셸이 Secret 에서 채울 env 이름 목록을 함께 돌려준다.

ingress.env 는 원가연 durable-ingress(접수 큐, sample-app
src/durable-ingress/server.js)를 운영자 PC compose 로 띄울 때 쓰는 env 파일이다
(설계 0.1 S15). 대상 이름은 클라우드 라벨과 같고, 제어 토큰 값은 담지 않는다.

  job = handoff.build_job_config(inventory, instance_id=..., ...)
  path, env_names = handoff.render_cloud_config(job, readiness, out_dir)
  path, env_names = handoff.render_ingress_env(job, targets, redis_url, out_dir)
"""

import dataclasses
import datetime
import json
import pathlib
import re
import urllib.parse
from typing import Literal

import yaml

from crosscloud import model

Side = Literal["source", "target"]
LoginRole = Literal["admin", "app", "cdc", "sink", "verify"]
# 접수 큐가 원본을 가리키는지는 여기서 보지 않는다. ingress-start 가 이 파일
# 렌더링 뒤에 돌기 때문이다(순환). 그 조건은 원가연 PRECHECK 가 본다.
ReadinessName = Literal[
    "replication_flags",
    "migration_logins",
    "target_schema_empty",
    "profile_secrets",
]
READINESS_NAMES: tuple[ReadinessName, ...] = (
    "replication_flags",
    "migration_logins",
    "target_schema_empty",
    "profile_secrets",
)
CONFIG_FILE_NAME = "config.cloud.yaml"
INGRESS_ENV_FILE_NAME = "ingress.env"
INGRESS_CONTROL_TOKEN_ENV = "INGRESS_CONTROL_TOKEN"
# durable-ingress 는 처음 뜰 때 'onprem' 대상이 있어야 한다(service.js
# createIngress 의 initialTarget 기본값). 원본 앱 주소를 같은 이름으로 둔다.
_INGRESS_INITIAL_TARGET = "onprem"

# migration.cloud 가 쪽마다 실제로 접속하는 역할. 원본은 CDC 로 읽고 대상은
# sink 로 쓴다. app 은 이름만 쓰고(소유자 검사·트리거) 비밀번호는 읽지 않는다.
_SIDE_ROLES: dict[Side, tuple[LoginRole, ...]] = {
    "source": ("admin", "app", "cdc", "verify"),
    "target": ("admin", "app", "sink", "verify"),
}
_MIGRATION_USERS: dict[LoginRole, str] = {
    "admin": "migration_admin",
    "cdc": "migration_cdc",
    "sink": "migration_sink",
    "verify": "migration_verify",
}
# migration/cloud/plan.py 의 검사와 같은 규칙. 여기서 먼저 거절한다.
_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_JOB_ID = re.compile(r"[a-z][a-z0-9_]{0,39}")
_LOOPBACK_URL = re.compile(r"http://127\.0\.0\.1:\d{1,5}")


class NotReadyError(RuntimeError):
    """인계 전 점검(readiness) 항목이 빠졌거나 준비되지 않았다."""


@dataclasses.dataclass(frozen=True)
class DbEndpoint:
    """PostgreSQL 접속 주소. 라벨은 클라우드 이름이다."""

    cloud: model.Cloud
    host: str
    port: int
    database: str


@dataclasses.dataclass(frozen=True)
class MigrationLogin:
    """migration.cloud 가 쓰는 DB 로그인 하나. 비밀번호는 env 이름만."""

    role: LoginRole
    user: str
    password_env: str


@dataclasses.dataclass(frozen=True)
class AppTable:
    """옮길 앱 테이블 하나(소문자 schema.table)와 기본키 열."""

    name: str
    primary_key: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class IngressTarget:
    """접수 큐가 요청을 전달할 앱 하나(INGRESS_TARGETS 한 항목).

    Attributes:
        cloud: 라벨. 접수 큐의 대상 이름으로도 쓴다.
        url: 앱 요청 API 기준 주소(http·https, 인증 정보·쿼리 없음).
        token_env: 앱 요청 API Bearer 토큰 env 이름. 쓰면 https 만 된다.
    """

    cloud: model.Cloud
    url: str
    token_env: str | None = None


@dataclasses.dataclass(frozen=True)
class ReadinessItem:
    """인계 전 점검 한 항목. detail 에는 값·비밀을 담지 않는다."""

    name: ReadinessName
    ready: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class CloudJobConfig:
    """config.cloud.yaml 한 장의 내용. 값 없이 env 이름만 담는다.

    Attributes:
        job_id: 방향마다 새 값. 소문자 40자 이하.
        source: 원본 DB. 라벨은 source.cloud.
        target: 대상 DB. 라벨은 target.cloud.
        source_logins: 원본 쪽 로그인(admin·app·cdc·verify).
        target_logins: 대상 쪽 로그인(admin·app·sink·verify).
        sslmode: 본선은 require(설계 0.1 S7).
        tables: 옮길 앱 테이블 전체. 완전성은 운영자가 확인한다.
        control_url: 제어 어댑터 주소. 127.0.0.1 에서만 듣는다.
        connect_url: Kafka Connect REST 주소.
    """

    job_id: str
    source: DbEndpoint
    target: DbEndpoint
    source_logins: tuple[MigrationLogin, ...]
    target_logins: tuple[MigrationLogin, ...]
    sslmode: Literal["require", "verify-full"]
    tables: tuple[AppTable, ...]
    control_url: str
    connect_url: str

    def __post_init__(self) -> None:
        """migration.cloud 가 거절할 설정을 먼저 거절한다.

        Raises:
            ValueError: 방향·job_id·주소·로그인·테이블이 규칙을 어길 때.
        """
        if self.source.cloud == self.target.cloud:
            raise ValueError("source 와 target 의 cloud 라벨이 같다")
        if not _JOB_ID.fullmatch(self.job_id):
            raise ValueError(f"job_id 규칙 위반: {self.job_id!r}")
        if not _LOOPBACK_URL.fullmatch(self.control_url):
            raise ValueError("control_url 은 http://127.0.0.1:<port> 여야 한다")
        if not self.connect_url.startswith(("http://", "https://")):
            raise ValueError("connect_url 은 http(s) 주소여야 한다")
        _check_logins("source", self.source_logins)
        _check_logins("target", self.target_logins)
        _check_tables(self.tables)


def new_job_id(
    source: model.Cloud, target: model.Cloud, now: datetime.datetime
) -> str:
    """방향과 시각으로 새 job_id 를 만든다(예: ccx_aws_gcp_20261107153000)."""
    return f"ccx_{source}_{target}_{now:%Y%m%d%H%M%S}"


def standard_logins(
    cloud: model.Cloud, side: Side, app_user: str
) -> tuple[MigrationLogin, ...]:
    """한 쪽 DB 의 표준 로그인 이름과 env 이름을 만든다.

    env 이름은 라벨 기준(<CLOUD>_<ROLE>_PASSWORD)이라 방향이 바뀌어도 같다.
    DB 로그인을 만드는 쪽(datapath)도 이 이름을 쓴다.

    Args:
        cloud: DB 가 있는 클라우드 라벨.
        side: 이 job 에서 원본인지 대상인지.
        app_user: 앱 DB 로그인(원본 앱 사용자 재사용).

    Returns:
        그 쪽에서 migration.cloud 가 쓰는 역할의 로그인.
    """
    return tuple(
        MigrationLogin(
            role=role,
            user=app_user if role == "app" else _MIGRATION_USERS[role],
            password_env=f"{cloud.upper()}_{role.upper()}_PASSWORD",
        )
        for role in _SIDE_ROLES[side]
    )


def build_job_config(
    inventory: model.Inventory,
    *,
    instance_id: str,
    workload: str,
    target: DbEndpoint,
    tables: tuple[AppTable, ...],
    control_url: str,
    connect_url: str,
    now: datetime.datetime,
) -> CloudJobConfig:
    """원본 Inventory 와 대상 정보로 job 설정을 만든다.

    원본 DB 주소는 Inventory 의 postgres_instance 자원 속성(host·port·
    database)에서, 앱 로그인은 워크로드의 db_user 에서 읽는다.

    Raises:
        ValueError: 자원·워크로드·속성이 없거나 규칙을 어길 때.
    """
    source = _source_endpoint(inventory, instance_id)
    app_user = _app_user(inventory, workload)
    return CloudJobConfig(
        job_id=new_job_id(source.cloud, target.cloud, now),
        source=source,
        target=target,
        source_logins=standard_logins(source.cloud, "source", app_user),
        target_logins=standard_logins(target.cloud, "target", app_user),
        sslmode="require",
        tables=tables,
        control_url=control_url,
        connect_url=connect_url,
    )


def render_cloud_config(
    job: CloudJobConfig,
    readiness: tuple[ReadinessItem, ...],
    out_dir: pathlib.Path,
) -> tuple[pathlib.Path, tuple[str, ...]]:
    """점검을 다 통과했을 때만 config.cloud.yaml 을 쓴다.

    Args:
        job: 쓸 job 설정.
        readiness: READINESS_NAMES 네 항목의 점검 결과.
        out_dir: 파일을 둘 디렉토리.

    Returns:
        쓴 파일 경로와, 운영자 셸·Connect 가 채워야 할 env 이름(정렬).

    Raises:
        NotReadyError: 항목이 빠졌거나 하나라도 준비되지 않았을 때.
    """
    _require_ready(readiness)
    document = {
        "targets": {
            job.source.cloud: _target_doc(job.source, job, job.source_logins),
            job.target.cloud: _target_doc(job.target, job, job.target_logins),
        },
        "job": {
            "job_id": job.job_id,
            "source": job.source.cloud,
            "target": job.target.cloud,
        },
        "application": {
            "control_url": job.control_url,
            "tables": [
                {"name": t.name, "primary_key": list(t.primary_key)}
                for t in job.tables
            ],
        },
        "kafka": {"connect_url": job.connect_url},
    }
    path = out_dir / CONFIG_FILE_NAME
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    env_names = sorted(
        login.password_env
        for login in job.source_logins + job.target_logins
        if login.role != "app"
    )
    return path, tuple(env_names)


def _source_endpoint(
    inventory: model.Inventory, instance_id: str
) -> DbEndpoint:
    for resource in inventory.resources:
        if (
            resource.kind == "postgres_instance"
            and resource.native_id == instance_id
        ):
            attributes = {kv.key: kv.value for kv in resource.attributes}
            return DbEndpoint(
                cloud=resource.cloud,
                host=str(_attribute(attributes, "host", instance_id)),
                port=int(_attribute(attributes, "port", instance_id)),
                database=str(_attribute(attributes, "database", instance_id)),
            )
    raise ValueError(f"Inventory 에 postgres_instance 가 없다: {instance_id}")


def _attribute(
    attributes: dict[str, object], key: str, instance_id: str
) -> object:
    value = attributes.get(key)
    if value is None or value == "":
        raise ValueError(f"{instance_id}: 속성 {key} 가 없다")
    return value


def _app_user(inventory: model.Inventory, workload: str) -> str:
    for candidate in inventory.workloads:
        if candidate.name == workload:
            if not candidate.db_user:
                raise ValueError(f"워크로드 {workload} 의 db_user 가 없다")
            return candidate.db_user
    raise ValueError(f"Inventory 에 워크로드가 없다: {workload}")


def _check_logins(side: Side, logins: tuple[MigrationLogin, ...]) -> None:
    roles = tuple(login.role for login in logins)
    if sorted(roles) != sorted(_SIDE_ROLES[side]):
        raise ValueError(f"{side} 로그인 역할이 맞지 않다: {roles}")
    # plan.py 는 없는 역할을 기본 이름(migration_cdc 등)으로 보고 비교한다.
    users = {login.role: login.user for login in logins}
    others = {
        users.get(role, _MIGRATION_USERS[role])
        for role in ("admin", "cdc", "sink")
    }
    if users["app"] in others:
        raise ValueError(f"{side} app 로그인 이름이 이전용 로그인과 겹친다")


def _check_tables(tables: tuple[AppTable, ...]) -> None:
    if not tables or len({t.name for t in tables}) != len(tables):
        raise ValueError("tables 는 비어 있지 않고 이름이 겹치지 않아야 한다")
    for table in tables:
        parts = table.name.split(".")
        if len(parts) != 2 or not all(_IDENT.fullmatch(p) for p in parts):
            raise ValueError(f"table 이름은 소문자 schema.table: {table.name}")
        key = table.primary_key
        if not key or len(set(key)) != len(key):
            raise ValueError(f"table {table.name} 의 primary_key 가 잘못됐다")
        if not all(_IDENT.fullmatch(column) for column in key):
            raise ValueError(f"table {table.name} 의 primary_key 열 이름 위반")


def _target_doc(
    endpoint: DbEndpoint,
    job: CloudJobConfig,
    logins: tuple[MigrationLogin, ...],
) -> dict[str, object]:
    return {
        "host": endpoint.host,
        "port": endpoint.port,
        "dbname": endpoint.database,
        "sslmode": job.sslmode,
        "users": {
            login.role: {"user": login.user, "password_env": login.password_env}
            for login in logins
        },
    }


def _require_ready(readiness: tuple[ReadinessItem, ...]) -> None:
    by_name = {item.name: item for item in readiness}
    for name in READINESS_NAMES:
        item = by_name.get(name)
        if item is None:
            raise NotReadyError(f"readiness 항목이 없다: {name}")
        if not item.ready:
            raise NotReadyError(f"readiness 미충족: {name} ({item.detail})")


def render_ingress_env(
    job: CloudJobConfig,
    targets: tuple[IngressTarget, ...],
    redis_url: str,
    out_dir: pathlib.Path,
) -> tuple[pathlib.Path, tuple[str, ...]]:
    """durable-ingress 실행용 env 파일(ingress.env)을 쓴다. 비밀값은 없다.

    INGRESS_CONTROL_HOST 는 0.0.0.0 이다 — compose 컨테이너 안에서 듣고, 포트는
    호스트 127.0.0.1 에만 공개한다. 그래서 제어 토큰을 함께 쓴다.

    Args:
        job: 라벨(원본·대상)을 정하는 job 설정.
        targets: 두 라벨의 앱 주소.
        redis_url: 접수 큐가 쓸 Redis 주소(redis:// 또는 rediss://).
        out_dir: 파일을 둘 디렉토리.

    Returns:
        쓴 파일 경로와, 운영자 셸이 채워야 할 비밀 env 이름(정렬).

    Raises:
        ValueError: 라벨이 맞지 않거나 주소·토큰 전송이 안전하지 않을 때.
    """
    by_label = {target.cloud: target for target in targets}
    for label in (job.source.cloud, job.target.cloud):
        if label not in by_label:
            raise ValueError(f"ingress 대상에 {label} 이 없다")
    for target in targets:
        _check_ingress_url(target)
    if not redis_url.startswith(("redis://", "rediss://")):
        raise ValueError("REDIS_URL 은 redis:// 또는 rediss:// 여야 한다")
    entries = {label: _ingress_entry(by_label[label]) for label in by_label}
    entries[_INGRESS_INITIAL_TARGET] = _ingress_entry(
        by_label[job.source.cloud]
    )
    lines = (
        "# crosscloud 가 렌더링한 durable-ingress env. 비밀값 없음.",
        f"# {INGRESS_CONTROL_TOKEN_ENV} 는 운영자 셸 env 로 넘긴다.",
        "INGRESS_TARGETS=" + json.dumps(entries, separators=(",", ":")),
        f"REDIS_URL={redis_url}",
        "INGRESS_CONTROL_HOST=0.0.0.0",
    )
    path = out_dir / INGRESS_ENV_FILE_NAME
    path.write_text("\n".join(lines) + "\n")
    secret_envs = {INGRESS_CONTROL_TOKEN_ENV}
    secret_envs.update(t.token_env for t in targets if t.token_env)
    return path, tuple(sorted(secret_envs))


def _ingress_entry(target: IngressTarget) -> dict[str, str]:
    entry = {"url": target.url}
    if target.token_env:
        entry["token_env"] = target.token_env
    return entry


def _check_ingress_url(target: IngressTarget) -> None:
    # service.js createIngress 와 같은 규칙: http(s), 인증 정보·쿼리·조각 없음,
    # 토큰을 쓰면 https.
    parts = urllib.parse.urlsplit(target.url)
    unsafe = (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    )
    if unsafe:
        raise ValueError(f"{target.cloud} ingress 대상 url 이 안전하지 않다")
    if target.token_env and parts.scheme != "https":
        raise ValueError(f"{target.cloud}: 토큰을 쓰는 대상은 https 만 된다")


def app_tls_env(db_ca_pem: str) -> tuple[model.KeyValue, ...]:
    """대상 앱 배포에 넣을 DB TLS env(비밀 아님). 앱–DB 구간도 TLS 를 쓴다.

    sample-app(packages/request-store/config.js)은 PGSSLMODE 가 먼저이고,
    disable 이 아닌 모든 모드에서 인증서와 호스트 이름을 검증한다
    (rejectUnauthorized). CA 는 DB_SSL_CA(PEM 내용) 또는 PGSSLROOTCERT(파일
    경로)로 받는다. 이미지를 바꾸지 않으므로 파일이 아닌 DB_SSL_CA 를 쓴다.
    DATABASE_URL 에는 sslmode 밖의 ssl* 쿼리를 넣으면 앱이 기동을 거부한다.

    Args:
        db_ca_pem: 대상 DB 서버 CA 묶음 PEM(RDS 리전 번들·Cloud SQL 서버 CA).

    Raises:
        ValueError: PEM 인증서가 아닐 때.
    """
    if "-----BEGIN CERTIFICATE-----" not in db_ca_pem:
        raise ValueError("db_ca_pem 은 PEM 인증서여야 한다")
    return (
        model.KeyValue("PGSSLMODE", "verify-full"),
        model.KeyValue("DB_SSL_CA", db_ca_pem),
    )
