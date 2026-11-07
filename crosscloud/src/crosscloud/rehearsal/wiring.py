"""Terraform 출력·설정 → 다음 단계 입력. 순수 함수만 둔다.

출력(outputs)은 crosscloud.provision.terraform.Terraform.outputs() 의 모양
(이름 → value)이다. 이름 대응을 여기 한 곳에만 두어 사람이 출력을 손으로
옮기지 않게 한다.

  handoff = wiring.forward_handoff(cfg, aws=..., gcp_base=..., gcp_app=...)
  argv = wiring.render_handoff_argv(cfg, handoff, paths.job("forward"))

스택 이름: aws(첫 AWS 스택 — 순방향 원본·귀환 대상 앱), aws_return(귀환 대상
새 RDS), registry·target_base·target_app(GCP), audit(감사 버킷).
"""

import dataclasses
from collections.abc import Mapping
from typing import Any

from crosscloud import datapath, handoff, pg_client
from crosscloud.rehearsal import config

Outputs = Mapping[str, Any]

# 운영자 PC 의 고정 주소(deploy/operator-pc compose 와 같다).
CONTROL_URL = "http://127.0.0.1:18080"
CONNECT_URL = "http://127.0.0.1:8083"
INGRESS_CONTROL_URL = "http://127.0.0.1:9092"
# 접수 큐가 compose 망 안에서 보는 Redis. 정리 확인은 호스트 쪽을 본다.
INGRESS_REDIS_URL = "redis://redis:6379"
HOST_REDIS_URL = "redis://127.0.0.1:6379"
_POSTGRES_PORT = 5432
_APP_IMAGE_NAME = "sample-app"
# 프로필 A(원본 DB 접속 URL)·B·C 와 원가연 로그인 비밀번호 그릇(접두사 뒤).
AWS_SECRETS = (
    "database-url",
    "database-url-c",
    "aws-admin-password",
    "aws-cdc-password",
    "aws-verify-password",
)
AWS_RETURN_SECRETS = (
    "aws-admin-password",
    "aws-app-password",
    "aws-sink-password",
    "aws-verify-password",
)
GCP_SECRETS = (
    "database-url",
    "database-url-b",
    "postgres-password",
    "gcp-admin-password",
    "gcp-app-password",
    "gcp-cdc-password",
    "gcp-sink-password",
    "gcp-verify-password",
)
_APP_TLS_MODE = {"PGSSLMODE": "verify-full"}


@dataclasses.dataclass(frozen=True)
class Handoff:
    """render-handoff·prepare-target 에 넘길 한 job 의 입력.

    Attributes:
        source_tf: --source-tf 문서(`terraform output -json` 꼴).
        target_tf: --target-tf 문서.
        target_database: 대상 DB 이름.
        source_app_url: 원본 앱 공개 주소(ingress 대상).
        target_app_url: 대상 앱 공개 주소.
        platform: 대상 앱 플랫폼(cloud_run·ecs).
        service: 대상 앱 서비스(Cloud Run 전체 이름·ECS 서비스 이름).
        profile_secret: 대상 프로필 Secret(GCP 짧은 ID·AWS ARN).
        cluster: ECS 클러스터 ARN(ecs 만).
        region: ECS 리전(ecs 만).
    """

    source_tf: dict[str, Any]
    target_tf: dict[str, Any]
    target_database: str
    source_app_url: str
    target_app_url: str
    platform: str
    service: str
    profile_secret: str
    cluster: str | None = None
    region: str | None = None

    def deploy(self, *, profile_version: str | None) -> dict[str, Any]:
        """target-deploy.yaml 문서. Cloud Run 은 버전 번호로 고정한다."""
        ref: dict[str, str] = {"secret": self.profile_secret}
        if profile_version is not None:
            ref["version"] = str(profile_version)
        document: dict[str, Any] = {
            "platform": self.platform,
            "service": self.service,
        }
        if self.cluster is not None:
            document["cluster"] = self.cluster
            document["region"] = self.region
        document["secret_env"] = {"DATABASE_URL": ref}
        return document


def forward_handoff(
    cfg: config.RehearsalConfig,
    *,
    aws: Outputs,
    gcp_base: Outputs,
    gcp_app: Outputs,
) -> Handoff:
    """순방향(AWS→GCP) job 입력. 원본 RDS·ALB → 대상 Cloud SQL·Cloud Run."""
    return Handoff(
        source_tf=_tf_document(aws),
        target_tf=_tf_document(gcp_base),
        target_database=cfg.forward_database,
        source_app_url=_alb_url(aws),
        target_app_url=str(gcp_app["service_uri"]),
        platform="cloud_run",
        service=(
            f"projects/{cfg.gcp_project}/locations/{cfg.gcp_region}"
            f"/services/{gcp_app['service_name']}"
        ),
        profile_secret=str(gcp_base["secret_ids"]["database-url-b"]),
    )


def return_handoff(
    cfg: config.RehearsalConfig,
    *,
    aws: Outputs,
    aws_return: Outputs,
    gcp_base: Outputs,
    gcp_app: Outputs,
) -> Handoff:
    """귀환(GCP→AWS) job 입력. 대상 DB 는 새 RDS, 대상 앱은 첫 ECS 서비스다.

    대상 앱 자리는 순방향 원본 ECS·ALB 를 다시 쓴다(설계 3절 ⑥). 대상 프로필
    Secret 은 그 ECS 실행 역할이 읽는 첫 스택의 그릇이다.
    """
    return Handoff(
        source_tf=_tf_document(gcp_base),
        target_tf=_tf_document(aws_return),
        target_database=cfg.return_database,
        source_app_url=str(gcp_app["service_uri"]),
        target_app_url=_alb_url(aws),
        platform="ecs",
        service=str(aws["app"]["service_name"]),
        profile_secret=str(aws["secret_arns"]["database-url-c"]),
        cluster=str(aws["cluster_arn"]),
        region=cfg.aws_region,
    )


def render_handoff_argv(
    cfg: config.RehearsalConfig, result: Handoff, files: config.JobPaths
) -> list[str]:
    """`crosscloud render-handoff` 인자(명령 이름 포함)."""
    return [
        "render-handoff",
        "--inventory",
        str(files.discover),
        "--source-tf",
        str(files.source_tf),
        "--target-tf",
        str(files.target_tf),
        "--target-database",
        result.target_database,
        "--source-app-user",
        cfg.app_user,
        "--table",
        f"{cfg.table}={cfg.primary_key}",
        "--readiness",
        str(files.readiness),
        "--control-url",
        CONTROL_URL,
        "--connect-url",
        CONNECT_URL,
        "--source-app-url",
        result.source_app_url,
        "--target-app-url",
        result.target_app_url,
        "--redis-url",
        INGRESS_REDIS_URL,
        "--out-dir",
        str(files.out),
    ]


def forward_plans(
    cfg: config.RehearsalConfig, *, aws: Outputs, gcp_base: Outputs
) -> tuple[datapath.PrincipalPlan, datapath.PrincipalPlan]:
    """순방향 DB 로그인 준비 계획(원본 RDS, 대상 Cloud SQL)."""
    source = _plan(
        cfg,
        direction="aws_to_gcp",
        side="source",
        endpoint=_rds_endpoint(aws, cfg.source_database),
        secret_prefix=cfg.prefix,
        run_id=f"{cfg.run}fwd",
    )
    target = _plan(
        cfg,
        direction="aws_to_gcp",
        side="target",
        endpoint=_cloud_sql_endpoint(gcp_base, cfg.forward_database),
        secret_prefix=cfg.prefix,
        run_id=f"{cfg.run}fwd",
        profile_secret=f"{cfg.prefix}-database-url-b",
    )
    return source, target


def return_plans(
    cfg: config.RehearsalConfig, *, gcp_base: Outputs, aws_return: Outputs
) -> tuple[datapath.PrincipalPlan, datapath.PrincipalPlan]:
    """귀환 DB 로그인 준비 계획(원본 Cloud SQL, 대상 새 RDS)."""
    source = _plan(
        cfg,
        direction="gcp_to_aws",
        side="source",
        endpoint=_cloud_sql_endpoint(gcp_base, cfg.forward_database),
        secret_prefix=cfg.prefix,
        run_id=f"{cfg.run}ret",
    )
    target = _plan(
        cfg,
        direction="gcp_to_aws",
        side="target",
        endpoint=_rds_endpoint(aws_return, cfg.return_database),
        secret_prefix=cfg.return_prefix,
        run_id=f"{cfg.run}ret",
        profile_secret=f"{cfg.prefix}-database-url-c",
    )
    return source, target


def password_exports(
    cfg: config.RehearsalConfig, job: config.Job
) -> tuple[str, ...]:
    """원가연·Connect 가 읽을 비밀번호 env 를 Secret 에서 채우는 셸 줄.

    값이 아니라 명령이다. `eval "$(...)"` 할 때 셸 안에서만 값이 생긴다.
    """
    sides = _job_sides(cfg, job)
    lines = []
    for cloud, side, secret_prefix in sides:
        for login in handoff.standard_logins(cloud, side, cfg.app_user):
            if login.role == "app":
                continue
            secret_id = f"{secret_prefix}-{cloud}-{login.role}-password"
            lines.append(
                f'export {login.password_env}="$('
                f'{_secret_read_command(cfg, cloud, secret_id)})"'
            )
    return tuple(lines)


def aws_source_tfvars(
    cfg: config.RehearsalConfig,
    *,
    app_image: str | None = None,
    db_ssl_ca: str | None = None,
) -> dict[str, Any]:
    """첫 AWS 스택. 이미지가 없으면 기반만(ECR 을 먼저 만들어 복사하려고)."""
    tfvars: dict[str, Any] = {
        "name_prefix": cfg.prefix,
        "direction": "aws_to_gcp",
        "region": cfg.aws_region,
        "db_name": cfg.source_database,
        "secret_names": list(AWS_SECRETS),
        "include_app": app_image is not None,
    }
    if app_image is not None:
        tfvars["app_image"] = app_image
        tfvars["app_env"] = dict(_APP_TLS_MODE)
        if db_ssl_ca is not None:
            tfvars["app_db_ssl_ca"] = db_ssl_ca
    return tfvars


def aws_return_tfvars(cfg: config.RehearsalConfig) -> dict[str, Any]:
    """귀환 대상 AWS 스택(새 빈 RDS). 앱은 첫 스택의 ECS 를 다시 쓴다."""
    return {
        "name_prefix": cfg.return_prefix,
        "direction": "gcp_to_aws",
        "region": cfg.aws_region,
        "db_name": cfg.return_database,
        "secret_names": list(AWS_RETURN_SECRETS),
        "include_app": False,
        "app_secret_env": {},
    }


def registry_tfvars(cfg: config.RehearsalConfig) -> dict[str, Any]:
    """GCP Artifact Registry."""
    return _gcp_common(cfg)


def target_base_tfvars(cfg: config.RehearsalConfig) -> dict[str, Any]:
    """GCP Cloud SQL·Secret 그릇. 대상 DB 이름은 원본과 다르다."""
    return {
        **_gcp_common(cfg),
        "database_name": cfg.forward_database,
        "secret_names": list(GCP_SECRETS),
    }


def target_app_tfvars(
    cfg: config.RehearsalConfig,
    *,
    image: str,
    db_ssl_ca: str,
    database_url_version: str,
) -> dict[str, Any]:
    """Cloud Run 을 원본 프로필(A, 이관한 URL Secret 의 고정 버전)로 띄운다."""
    return {
        **_gcp_common(cfg),
        "image": image,
        "env": {**_APP_TLS_MODE, "DB_SSL_CA": db_ssl_ca},
        "secret_env": {
            "DATABASE_URL": {
                "secret": f"{cfg.prefix}-database-url",
                "version": database_url_version,
            }
        },
    }


def audit_tfvars(cfg: config.RehearsalConfig) -> dict[str, Any]:
    """감사 기록 보존 버킷 2개."""
    return {**_gcp_common(cfg), "aws_region": cfg.aws_region}


def copied_image(repository_url: str, source_image: str) -> tuple[str, str]:
    """복사본의 (태그 ref, digest ref). 단일 원본 복사는 digest 를 보존한다.

    ECR 저장소는 그 자체가 이미지 이름이고, Artifact Registry 저장소 아래에는
    이미지 이름을 하나 더 붙인다.
    """
    digest = source_image.split("@", 1)[1]
    name = str(repository_url)
    if ".pkg.dev/" in name:
        name = f"{name}/{_APP_IMAGE_NAME}"
    return f"{name}:src-{digest[7:19]}", f"{name}@{digest}"


def registry_host(repository_url: str) -> str:
    """Docker login 에 줄 레지스트리 호스트."""
    return str(repository_url).split("/", 1)[0]


def _tf_document(outputs: Outputs) -> dict[str, Any]:
    return {name: {"value": value} for name, value in outputs.items()}


def _alb_url(aws: Outputs) -> str:
    return f"http://{aws['app']['alb_dns_name']}"


def _rds_endpoint(aws: Outputs, database: str) -> pg_client.DbEndpoint:
    db = aws["db"]
    return pg_client.DbEndpoint(str(db["address"]), db["port"], database)


def _cloud_sql_endpoint(
    gcp_base: Outputs, database: str
) -> pg_client.DbEndpoint:
    # 공개 IP 로 붙는다(handoff.db_from_terraform 과 같다). 앱 verify-full 이
    # 이 주소로 통하는지는 실행 전 확인 항목이다(설계 0.1 S7, X-T3p).
    return pg_client.DbEndpoint(
        str(gcp_base["db_public_ip"]), _POSTGRES_PORT, database
    )


def _plan(
    cfg: config.RehearsalConfig,
    *,
    direction: str,
    side: datapath.Side,
    endpoint: pg_client.DbEndpoint,
    secret_prefix: str,
    run_id: str,
    profile_secret: str | None = None,
) -> datapath.PrincipalPlan:
    source, target = direction.split("_to_")
    cloud = source if side == "source" else target
    names = {
        login.role: login.user
        for login in handoff.standard_logins(cloud, side, cfg.app_user)
    }
    logins = tuple(
        datapath.DbLogin(
            purpose,
            names[purpose],
            f"{secret_prefix}-{cloud}-{purpose}-password",
        )
        for purpose in sorted(datapath.REQUIRED_PURPOSES[side])
    )
    return datapath.PrincipalPlan(
        direction=direction,  # type: ignore[arg-type]
        side=side,
        endpoint=endpoint,
        logins=logins,
        tables=(cfg.table,),
        source_app_user=cfg.app_user,
        run_id=run_id,
        app_profile_secret_id=profile_secret,
    )


def _job_sides(
    cfg: config.RehearsalConfig, job: config.Job
) -> tuple[tuple[str, handoff.Side, str], ...]:
    if job == "forward":
        return (
            ("aws", "source", cfg.prefix),
            ("gcp", "target", cfg.prefix),
        )
    return (
        ("gcp", "source", cfg.prefix),
        ("aws", "target", cfg.return_prefix),
    )


def _secret_read_command(
    cfg: config.RehearsalConfig, cloud: str, secret_id: str
) -> str:
    if cloud == "aws":
        return (
            f"aws secretsmanager get-secret-value --region {cfg.aws_region}"
            f" --secret-id {secret_id} --query SecretString --output text"
        )
    return (
        f"gcloud secrets versions access latest --project {cfg.gcp_project}"
        f" --secret {secret_id}"
    )


def _gcp_common(cfg: config.RehearsalConfig) -> dict[str, Any]:
    return {
        "project_id": cfg.gcp_project,
        "region": cfg.gcp_region,
        "name_prefix": cfg.prefix,
        "direction": "aws_to_gcp",
    }
