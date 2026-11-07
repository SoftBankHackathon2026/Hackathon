"""리허설 단계. 단계마다 상태 디렉터리에 산출물과 완료 표시를 남긴다.

자동 단계(Terraform·이미지 복사·DB 준비·인계 렌더링·정리)는 여기서 돌리고,
전환(원가연 run·승인)은 commands 가 만든 명령을 사람이 돌린다. 끝난 단계는
다시 돌리지 않는다(--force 로만). 단계 안의 동작은 다시 돌려도 같은 결과다
(Terraform apply·datapath·같은 run_id 의 Secret 이관·digest 복사).
"""

import contextlib
import dataclasses
import datetime
import json
import pathlib
import shlex
import shutil
from collections.abc import Callable, Mapping
from typing import Any

import boto3
import google.auth
import yaml
from google.auth.transport import requests as google_requests
from google.cloud import secretmanager

from crosscloud import cli as crosscloud_cli
from crosscloud import cutover_stage, datapath, pg_client
from crosscloud.provision import residue_aws, residue_gcp
from crosscloud.rehearsal import commands, config, dbprep, runner, wiring
from crosscloud.secret_transfer import run as secret_run
from crosscloud.secret_transfer import store_aws, store_gcp

_TERRAFORM = commands.CROSSCLOUD_DIR / "terraform"
MODULES = {
    "aws": _TERRAFORM / "aws",
    "aws_return": _TERRAFORM / "aws",
    "registry": _TERRAFORM / "google" / "registry",
    "target_base": _TERRAFORM / "google" / "target_base",
    "target_app": _TERRAFORM / "google" / "target_app",
    "audit": _TERRAFORM / "audit",
}
# 만든 순서의 역순. 앱 → 감사 → 귀환 DB → 대상 DB → 레지스트리 → 원본.
DESTROY_ORDER = (
    "target_app",
    "audit",
    "aws_return",
    "target_base",
    "registry",
    "aws",
)
_TOOLS = ("terraform", "docker", "aws", "gcloud", "curl")
_RDS_CA_URL = (
    "https://truststore.pki.rds.amazonaws.com/{region}/{region}-bundle.pem"
)
# pg_dump·psql 은 서버(RDS·Cloud SQL 모두 PG16)와 같은 주 버전 컨테이너로 돈다.
PG_TOOLS = pg_client.PgTools(
    command_prefix=(
        "docker",
        "run",
        "--rm",
        "-i",
        "-e",
        "PGPASSWORD",
        "-e",
        "PGSSLMODE",
        "-e",
        "PGCONNECT_TIMEOUT",
        "postgres:16",
    )
)
_SESSION_LABEL = "com.ghost.crosscloud.session"


@dataclasses.dataclass(frozen=True)
class Context:
    """단계 하나가 보는 것 — 설정, 상태 자리, 실행기."""

    cfg: config.RehearsalConfig
    paths: config.StatePaths
    runner: runner.Runner
    dry: bool
    echo: Callable[[str], None] = print

    @classmethod
    def open(
        cls,
        state: pathlib.Path,
        step_runner: runner.Runner,
        *,
        dry: bool,
        echo: Callable[[str], None] = print,
    ) -> "Context":
        """상태 디렉터리의 설정을 읽어 연다.

        Raises:
            runner.StepError: init 을 하지 않았을 때.
        """
        paths = config.StatePaths(state)
        if not paths.config.exists():
            raise runner.StepError(f"설정이 없다 — 먼저 init: {paths.config}")
        return cls(config.load(paths.config), paths, step_runner, dry, echo)

    def outputs(self, stack: str) -> Mapping[str, Any]:
        """앞 단계가 남긴 Terraform 출력. dry-run 이면 자리표시로 대신한다."""
        path = self.paths.outputs(stack)
        if path.exists():
            return json.loads(path.read_text())
        if self.dry:
            return runner.Placeholder(stack)  # type: ignore[return-value]
        raise runner.StepError(f"{stack} 출력이 없다 — 그 스택 단계를 먼저")

    def text(self, path: pathlib.Path) -> str:
        """앞 단계가 남긴 파일(비밀 아님)."""
        if path.exists():
            return path.read_text()
        if self.dry:
            return runner.Placeholder(path.name)
        raise runner.StepError(f"{path.name} 이 없다 — 앞 단계를 먼저")

    def value(self, key: str) -> str:
        """앞 단계가 values.json 에 남긴 값."""
        values = self._values()
        if key in values:
            return values[key]
        if self.dry:
            return runner.Placeholder(key)
        raise runner.StepError(f"값 {key} 가 없다 — 앞 단계를 먼저")

    def remember(self, **values: str) -> None:
        """다음 단계에 넘길 비밀 아닌 값을 남긴다."""
        merged = {**self._values(), **values}
        self.runner.write(self.paths.values, json.dumps(merged, indent=2))

    def apply(self, stack: str, tfvars: Mapping[str, Any]) -> Mapping[str, Any]:
        """스택 하나를 적용한다."""
        return self.runner.apply(stack, MODULES[stack], tfvars)

    def _values(self) -> dict[str, str]:
        path = self.paths.values
        return json.loads(path.read_text()) if path.exists() else {}


@dataclasses.dataclass(frozen=True)
class Step:
    """이름 붙은 단계 하나."""

    name: str
    run: Callable[[Context], None]


def run_step(ctx: Context, step: Step, *, force: bool) -> None:
    """끝나지 않은 단계만 돌리고 완료 표시를 남긴다(dry-run 은 남기지 않음)."""
    done = ctx.paths.done(step.name)
    if done.exists() and not force:
        ctx.echo(f"{step.name}: 이미 끝났다 — 다시 하려면 --force")
        return
    ctx.echo(f"== {step.name}{' (dry-run)' if ctx.dry else ''}")
    step.run(ctx)
    if not ctx.dry:
        done.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.UTC).isoformat()
        done.write_text(json.dumps({"finished": stamp}) + "\n")


def preflight(ctx: Context) -> None:
    """0. 도구·신원·리전·접두사 확인. 바꾸는 것이 없다."""
    cfg = ctx.cfg
    ctx.echo(
        f"  AWS {cfg.aws_account}/{cfg.aws_region} · GCP {cfg.gcp_project}"
        f"/{cfg.gcp_region} · 접두사 {cfg.prefix}, {cfg.return_prefix}"
    )
    missing = [tool for tool in _TOOLS if shutil.which(tool) is None]
    identity = ctx.runner.sh(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        capture=True,
    )
    project = ctx.runner.sh(
        ["gcloud", "config", "get-value", "project"], capture=True
    )
    # ADC(라이브러리 신원)가 있는지만 본다. 토큰은 버린다.
    ctx.runner.sh(
        ["gcloud", "auth", "application-default", "print-access-token"],
        capture=True,
    )
    ctx.runner.sh(["docker", "buildx", "version"])
    problems = preflight_problems(cfg, identity, project, missing)
    if problems:
        raise runner.StepError("; ".join(problems))


def preflight_problems(
    cfg: config.RehearsalConfig,
    identity_json: str,
    project: str,
    missing_tools: list[str],
) -> list[str]:
    """사전 점검 판정. dry-run 자리표시는 판정하지 않는다."""
    problems = [f"도구 없음: {tool}" for tool in missing_tools]
    if not isinstance(identity_json, runner.Placeholder):
        account = json.loads(identity_json).get("Account")
        if account != cfg.aws_account:
            problems.append(f"AWS 계정 {account} ≠ {cfg.aws_account}")
    if not isinstance(project, runner.Placeholder):
        if project.strip() != cfg.gcp_project:
            problems.append(
                f"GCP 프로젝트 {project.strip()} ≠ {cfg.gcp_project}"
            )
    return problems


def aws_source(ctx: Context) -> None:
    """1. AWS 원본 고정물 — 기반 → 이미지 복사 → 시험 데이터 → 앱."""
    cfg = ctx.cfg
    base = ctx.apply("aws", wiring.aws_source_tfvars(cfg))
    repository = base["ecr_repository_url"]
    ctx.runner.pipe(
        ["aws", "ecr", "get-login-password", "--region", cfg.aws_region],
        [
            "docker",
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            wiring.registry_host(repository),
        ],
    )
    image = _copy_image(ctx, repository)
    rds_ca = ctx.runner.sh(
        ["curl", "-fsS", _RDS_CA_URL.format(region=cfg.aws_region)],
        capture=True,
    )
    ctx.runner.write(ctx.paths.rds_ca, rds_ca)
    rows = ctx.runner.call(
        "원본 DB 시험 데이터 씨앗(표·앱 로그인·URL Secret)",
        lambda: _seed(ctx, base),
        runner.Placeholder("씨앗 행 수"),
    )
    ctx.echo(f"  시험 행 {rows}개")
    ctx.apply(
        "aws", wiring.aws_source_tfvars(cfg, app_image=image, db_ssl_ca=rds_ca)
    )
    ctx.remember(aws_image=image)


def gcp_target(ctx: Context) -> None:
    """2. GCP 대상 기반 — 레지스트리·이미지 복사·Cloud SQL·CA 합본."""
    cfg = ctx.cfg
    registry = ctx.apply("registry", wiring.registry_tfvars(cfg))
    repository = registry["repository_url"]
    ctx.runner.pipe(
        ["gcloud", "auth", "print-access-token"],
        [
            "docker",
            "login",
            "--username",
            "oauth2accesstoken",
            "--password-stdin",
            "https://" + wiring.registry_host(repository),
        ],
    )
    image = _copy_image(ctx, repository)
    base = ctx.apply("target_base", wiring.target_base_tfvars(cfg))
    cloud_sql_ca = ctx.runner.sh(
        [
            "gcloud",
            "sql",
            "ssl",
            "server-ca-certs",
            "list",
            f"--instance={base['db_instance_name']}",
            f"--project={cfg.gcp_project}",
            "--format=value(cert)",
        ],
        capture=True,
    )
    bundle = ctx.text(ctx.paths.rds_ca).rstrip("\n") + "\n" + cloud_sql_ca
    ctx.runner.write(ctx.paths.db_ca, bundle)
    ctx.runner.call(
        "Cloud SQL postgres 비밀번호 준비(Secret 이 정본)",
        lambda: _gcp_master(ctx, base, "postgres"),
        None,
    )
    ctx.remember(gcp_image=image)


def prepare_forward(ctx: Context) -> None:
    """3. Secret 이관 · DB 로그인 준비 · Cloud Run(원본 프로필) · readiness."""
    cfg = ctx.cfg
    aws, base = ctx.outputs("aws"), ctx.outputs("target_base")
    version = ctx.runner.call(
        f"Secret 이관 AWS→GCP {cfg.prefix}-database-url(SHA-256 대조)",
        lambda: _transfer_profile_a(ctx),
        runner.Placeholder("이관 버전"),
    )
    plans = wiring.forward_plans(cfg, aws=aws, gcp_base=base)
    ctx.runner.call(
        "원본 RDS 원가연 로그인(admin·cdc·verify)",
        lambda: _prepare(
            plans[0], _aws_master(ctx, aws, "postgres"), _aws_store(cfg)
        ),
        None,
    )
    ctx.runner.call(
        f"대상 Cloud SQL {cfg.forward_database} 로그인·스키마·프로필 B",
        lambda: _prepare(
            plans[1],
            _gcp_master(ctx, base, "postgres"),
            _gcp_store(cfg),
            _aws_master(ctx, aws, cfg.source_database),
        ),
        None,
    )
    profile_b = _latest_gcp_version(ctx, f"{cfg.prefix}-database-url-b")
    ctx.apply(
        "target_app",
        wiring.target_app_tfvars(
            cfg,
            image=ctx.value("gcp_image"),
            db_ssl_ca=ctx.text(ctx.paths.db_ca),
            database_url_version=version,
        ),
    )
    _write_readiness(
        ctx,
        "forward",
        lambda: dbprep.collect_readiness(
            _aws_master(ctx, aws, cfg.source_database),
            _gcp_master(ctx, base, cfg.forward_database),
            plans,
            _gcp_store(cfg),
        ),
    )
    ctx.remember(profile_b_version=profile_b)


def handoff_forward(ctx: Context) -> None:
    """4. Terraform 출력 → render-handoff(순방향)."""
    aws = ctx.outputs("aws")
    base, app = ctx.outputs("target_base"), ctx.outputs("target_app")
    result = wiring.forward_handoff(
        ctx.cfg, aws=aws, gcp_base=base, gcp_app=app
    )
    discover = [
        "discover",
        "--direction",
        "aws_to_gcp",
        "--region",
        ctx.cfg.aws_region,
    ]
    _render_handoff(
        ctx, "forward", result, discover, ctx.value("profile_b_version")
    )


def prepare_return(ctx: Context) -> None:
    """7a. 귀환 대상 새 RDS · 양쪽 로그인 준비 · readiness."""
    cfg = ctx.cfg
    base = ctx.outputs("target_base")
    target = ctx.apply("aws_return", wiring.aws_return_tfvars(cfg))
    plans = wiring.return_plans(cfg, gcp_base=base, aws_return=target)
    ctx.runner.call(
        f"원본 Cloud SQL {cfg.forward_database} 로그인(admin·cdc·verify)",
        lambda: _prepare(
            plans[0], _gcp_master(ctx, base, "postgres"), _gcp_store(cfg)
        ),
        None,
    )
    ctx.runner.call(
        f"대상 RDS {cfg.return_database} 로그인·스키마·프로필 C",
        lambda: _prepare(
            plans[1],
            _aws_master(ctx, target, "postgres"),
            _aws_store(cfg),
            _gcp_master(ctx, base, cfg.forward_database),
        ),
        None,
    )
    _write_readiness(
        ctx,
        "return",
        lambda: dbprep.collect_readiness(
            _gcp_master(ctx, base, cfg.forward_database),
            _aws_master(ctx, target, cfg.return_database),
            plans,
            _aws_store(cfg),
        ),
    )


def handoff_return(ctx: Context) -> None:
    """7b. Terraform 출력 → render-handoff(귀환)."""
    result = wiring.return_handoff(
        ctx.cfg,
        aws=ctx.outputs("aws"),
        aws_return=ctx.outputs("aws_return"),
        gcp_base=ctx.outputs("target_base"),
        gcp_app=ctx.outputs("target_app"),
    )
    discover = [
        "discover",
        "--direction",
        "gcp_to_aws",
        "--project",
        ctx.cfg.gcp_project,
        "--region",
        ctx.cfg.gcp_region,
    ]
    _render_handoff(ctx, "return", result, discover, None)


def audit(ctx: Context) -> None:
    """8. 감사 보존 버킷을 만들고 이관 명령을 찍는다."""
    ctx.apply("audit", wiring.audit_tfvars(ctx.cfg))
    ctx.echo("  아래 명령을 확인해 채운 뒤 돌린다:")
    for line in commands.audit(ctx.cfg, ctx.paths):
        ctx.echo(line)


def cleanup(ctx: Context) -> None:
    """9. 봇 중지 → 미처리 0 → compose down → destroy 역순 → 잔여 0."""
    with contextlib.ExitStack() as locks:
        if not ctx.dry:
            for job in config.JOBS:
                state = ctx.paths.job(job).migration_state
                if state.is_dir():
                    locks.enter_context(cutover_stage.cloud_lock(state))
        _stop_operator_pc(ctx)
        remaining = {
            stack: ctx.runner.destroy(stack, MODULES[stack])
            for stack in DESTROY_ORDER
        }
    left = {stack: list(addrs) for stack, addrs in remaining.items() if addrs}
    residue = ctx.runner.call(
        f"잔여 질의 AWS {ctx.cfg.prefix}·{ctx.cfg.return_prefix},"
        f" GCP {ctx.cfg.prefix}",
        lambda: _residue(ctx),
        [],
    )
    docker_left = _docker_residue(ctx)
    for host in _registry_hosts(ctx):
        ctx.runner.sh(["docker", "logout", host])
    if left or residue or docker_left:
        raise runner.StepError(
            "잔여가 있다: "
            + json.dumps(
                {"state": left, "cloud": residue, "docker": docker_left},
                ensure_ascii=False,
            )
        )
    ctx.echo("  잔여 0")


AUTOMATED = (
    Step("preflight", preflight),
    Step("aws-source", aws_source),
    Step("gcp-target", gcp_target),
    Step("prepare-forward", prepare_forward),
    Step("handoff-forward", handoff_forward),
    Step("prepare-return", prepare_return),
    Step("handoff-return", handoff_return),
    Step("audit", audit),
    Step("cleanup", cleanup),
)


def _copy_image(ctx: Context, repository: str) -> str:
    tag, pinned = wiring.copied_image(repository, ctx.cfg.source_image)
    ctx.runner.sh(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            tag,
            ctx.cfg.source_image,
        ]
    )
    # digest 로 조회되면 같은 바이트가 그 저장소에 있다.
    ctx.runner.sh(["docker", "buildx", "imagetools", "inspect", pinned])
    return pinned


def _render_handoff(
    ctx: Context,
    job: config.Job,
    result: wiring.Handoff,
    discover: list[str],
    profile_version: str | None,
) -> None:
    files = ctx.paths.job(job)
    discover = [*discover, "--output", str(files.discover)]
    ctx.runner.call(
        "crosscloud " + shlex.join(discover),
        lambda: _crosscloud(discover, files, accept=(0, 3)),
        None,
    )
    ctx.runner.write(files.source_tf, json.dumps(result.source_tf, indent=2))
    ctx.runner.write(files.target_tf, json.dumps(result.target_tf, indent=2))
    ctx.runner.write(
        files.deploy,
        yaml.safe_dump(result.deploy(profile_version=profile_version)),
    )
    argv = wiring.render_handoff_argv(ctx.cfg, result, files)
    ctx.runner.call(
        "crosscloud " + shlex.join(argv),
        lambda: _crosscloud(argv, files, accept=(0,)),
        None,
    )


def _crosscloud(
    argv: list[str], files: config.JobPaths, *, accept: tuple[int, ...]
) -> None:
    files.out.mkdir(parents=True, exist_ok=True)
    code = crosscloud_cli.main(argv)
    if code not in accept:
        raise runner.StepError(f"crosscloud {argv[0]} 종료 코드 {code}")


def _write_readiness(
    ctx: Context, job: config.Job, collect: Callable[[], tuple]
) -> None:
    items = ctx.runner.call("readiness 네 항목 판정", collect, ())
    for item in items:
        mark = "ok" if item.ready else "미충족"
        ctx.echo(f"  {item.name}: {mark} — {item.detail}")
    ctx.runner.write(ctx.paths.job(job).readiness, dbprep.readiness_json(items))


def _stop_operator_pc(ctx: Context) -> None:
    env = commands.compose_env(ctx.cfg, ctx.paths)
    if not ctx.dry and not pathlib.Path(env["INGRESS_ENV_FILE"]).exists():
        # 인계 전에 멈췄다 — compose 를 띄운 적이 없다(ingress.env 가 없다).
        ctx.echo("  운영자 PC compose: 띄운 적 없음 — 건너뜀")
        return
    ingress = ["docker", "compose", "-f", str(commands.INGRESS_COMPOSE)]
    connect = ["docker", "compose", "-f", str(commands.CONNECT_COMPOSE)]
    ctx.runner.sh([*ingress, "stop", "traffic-bot"], env=env)
    drain = [
        "ingress-drain-check",
        "--ingress-control-url",
        wiring.INGRESS_CONTROL_URL,
        "--redis-url",
        wiring.HOST_REDIS_URL,
    ]
    ctx.runner.call(
        "crosscloud " + shlex.join(drain) + " (0 이 아니면 지우지 않는다)",
        lambda: _crosscloud(drain, ctx.paths.job("forward"), accept=(0,)),
        None,
    )
    ctx.runner.sh([*ingress, "down", "-v"], env=env)
    ctx.runner.sh([*connect, "down", "-v"], env=env)


def _docker_residue(ctx: Context) -> list[str]:
    label = f"label={_SESSION_LABEL}={ctx.cfg.run}"
    left = []
    for kind in (["ps", "-a"], ["volume", "ls"], ["network", "ls"]):
        listed = ctx.runner.sh(
            ["docker", *kind, "-q", "--filter", label], capture=True
        )
        if not isinstance(listed, runner.Placeholder):
            left += listed.split()
    return left


def _registry_hosts(ctx: Context) -> list[str]:
    hosts = []
    for stack, key in (
        ("aws", "ecr_repository_url"),
        ("registry", "repository_url"),
    ):
        if ctx.paths.outputs(stack).exists():
            hosts.append(wiring.registry_host(ctx.outputs(stack)[key]))
    return hosts


def _residue(ctx: Context) -> list[str]:
    cfg = ctx.cfg
    aws = residue_aws.AwsResidueScanner(boto3.client, region=cfg.aws_region)
    gcp = residue_gcp.GcpResidueScanner(project=cfg.gcp_project)
    items = [
        *aws.scan(cfg.prefix),
        *aws.scan(cfg.return_prefix),
        *gcp.scan(cfg.prefix),
    ]
    return [f"{item.cloud}:{item.kind}:{item.native_id}" for item in items]


def _seed(ctx: Context, base: Mapping[str, Any]) -> int:
    cfg = ctx.cfg
    return dbprep.seed_source(
        _aws_master(ctx, base, cfg.source_database),
        cfg,
        _aws_store(cfg),
        f"{cfg.prefix}-database-url",
    )


def _transfer_profile_a(ctx: Context) -> str:
    secret_id = f"{ctx.cfg.prefix}-database-url"
    results = secret_run.transfer_secrets(
        [secret_run.SecretTransferItem(secret_id, secret_id)],
        frozenset({secret_id}),
        _aws_store(ctx.cfg),
        _gcp_store(ctx.cfg),
        f"{ctx.cfg.run}fwd",
        target_name_prefix=ctx.cfg.prefix,
    )
    return _version_number(results[0].written.version_id)


def _latest_gcp_version(ctx: Context, secret_id: str) -> str:
    listed = ctx.runner.sh(
        [
            "gcloud",
            "secrets",
            "versions",
            "list",
            secret_id,
            f"--project={ctx.cfg.gcp_project}",
            "--filter=state=ENABLED",
            "--sort-by=~createTime",
            "--limit=1",
            "--format=value(name)",
        ],
        capture=True,
    )
    if isinstance(listed, runner.Placeholder):
        return listed
    if not listed.strip():
        raise runner.StepError(f"{secret_id} 에 사용 가능한 버전이 없다")
    return _version_number(listed.strip())


def _version_number(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _prepare(
    plan: datapath.PrincipalPlan,
    bootstrap: pg_client.PgLogin,
    secret_store: Any,
    schema_source: pg_client.PgLogin | None = None,
) -> None:
    datapath.prepare_db_principals(
        plan, bootstrap, secret_store, schema_source, PG_TOOLS
    )


def _aws_master(
    ctx: Context, outputs: Mapping[str, Any], database: str
) -> pg_client.PgLogin:
    db = outputs["db"]
    client = boto3.client("secretsmanager", region_name=ctx.cfg.aws_region)
    value = client.get_secret_value(SecretId=db["master_user_secret_arn"])
    endpoint = pg_client.DbEndpoint(db["address"], int(db["port"]), database)
    return dbprep.rds_master(value["SecretString"].encode(), endpoint)


def _gcp_master(
    ctx: Context, base: Mapping[str, Any], database: str
) -> pg_client.PgLogin:
    cfg = ctx.cfg
    credentials, _ = google.auth.default()
    setter = dbprep.cloud_sql_password_setter(
        google_requests.AuthorizedSession(credentials),
        cfg.gcp_project,
        base["db_instance_name"],
    )
    password = dbprep.ensure_bootstrap_password(
        _gcp_store(cfg), f"{cfg.prefix}-postgres-password", setter, cfg.run
    )
    endpoint = pg_client.DbEndpoint(base["db_public_ip"], 5432, database)
    return pg_client.PgLogin(endpoint, "postgres", password)


def _aws_store(cfg: config.RehearsalConfig) -> store_aws.AwsSecretStore:
    return store_aws.AwsSecretStore(
        boto3.client("secretsmanager", region_name=cfg.aws_region)
    )


def _gcp_store(cfg: config.RehearsalConfig) -> store_gcp.GcpSecretStore:
    return store_gcp.GcpSecretStore(
        secretmanager.SecretManagerServiceClient(), cfg.gcp_project
    )
