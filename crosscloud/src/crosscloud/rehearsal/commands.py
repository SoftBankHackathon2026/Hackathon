"""사람이 셸에서 돌릴 전환·감사 명령. 값이 아니라 명령 문자열만 만든다.

전환(원가연 run·승인)은 사람이 지켜보며 한 줄씩 돌린다. 경로는 상태
디렉터리에서 채우고, 비밀값은 `eval "$(... env <job>)"` 로 셸 안에서만
생긴다. 순서의 근거는 deploy/operator-pc/README.md 다.
"""

import pathlib
import shlex

from crosscloud.rehearsal import config, wiring

CROSSCLOUD_DIR = pathlib.Path(__file__).resolve().parents[3]
DEPLOY_DIR = CROSSCLOUD_DIR / "deploy" / "operator-pc"
MIGRATION_DIR = CROSSCLOUD_DIR.parent / "migration"
INGRESS_COMPOSE = DEPLOY_DIR / "ingress.compose.yaml"
CONNECT_COMPOSE = DEPLOY_DIR / "connect.compose.yaml"
CONNECT_RETURN_COMPOSE = DEPLOY_DIR / "connect.return.compose.yaml"


def approval_id(cfg: config.RehearsalConfig, job: config.Job) -> str:
    """원가연 run --approve 에 줄 승인 id."""
    return f"{cfg.prefix}-{job}"


def cutover(
    cfg: config.RehearsalConfig, paths: config.StatePaths, job: config.Job
) -> list[str]:
    """한 job 의 전환 명령(설계 3절 ⑤·⑥). 앞줄 # 은 설명이다."""
    files = paths.job(job)
    lines = _shell_setup(cfg, paths, job)
    lines += _start_ingress_side(job)
    lines += [
        f"mkdir -p {shlex.quote(str(files.migration_state))}",
        "# 접수 큐를 원본 라벨로 돌리고 전달을 연다.",
        _crosscloud("ingress-start", files, control=True, state=True)
        + " --allow-no-state",
        "# 앱 제어 어댑터(127.0.0.1:18080). 전환이 끝날 때까지 둔다.",
        _crosscloud("serve-adapter", files, control=True, state=False)
        + f" > {shlex.quote(str(files.root / 'adapter.log'))} 2>&1 &",
    ]
    if job == "forward":
        lines += [
            "# 트래픽 봇(접수 큐 8080 으로만 보낸다).",
            _compose(INGRESS_COMPOSE) + " --profile bot up -d traffic-bot",
        ]
    lines += [
        "# 복사 시작. READY 에서 종료 코드 3 으로 멈춘다.",
        _migration(files, "run"),
        "# READY 에서 대상 앱을 대상 프로필로 예열(대상 admin 비밀번호 env).",
        _crosscloud("prepare-target", files, control=False, state=True)
        + f" --deploy {shlex.quote(str(files.deploy))}"
        + f" --db-ca-file {shlex.quote(str(paths.db_ca))}",
        "# 승인 직전 확인. 종료 코드 0 이 아니면 승인하지 않는다.",
        _crosscloud("ingress-status", files, control=True, state=True),
        "# 승인. FENCING → … → SWITCHING → COMPLETED.",
        _migration(files, f"run --approve {approval_id(cfg, job)}"),
        "# COMPLETED 확인.",
        _migration(files, "status"),
    ]
    return lines


def audit(cfg: config.RehearsalConfig, paths: config.StatePaths) -> list[str]:
    """감사 기록 이관 명령. <...> 는 실행 전에 확인해 채운다."""
    receipts = paths.root / "audit"
    keys = receipts / f"keys-{cfg.aws_region}.json"
    target = f"{cfg.prefix}-audit"
    cli = "python -m crosscloud.audit_copy.cli"
    return [
        f"mkdir -p {shlex.quote(str(receipts))}",
        "# 실행 전 확인: CloudTrail 추적 버킷·접두사, 공개키 구간 시작 시각.",
        f"aws cloudtrail list-public-keys --region {cfg.aws_region}"
        f" --start-time <로그 기간 시작> > {shlex.quote(str(keys))}",
        f"{cli} aws-to-gcp --source-bucket <CloudTrail 버킷>"
        f" --source-prefix AWSLogs/{cfg.aws_account}/"
        f" --target-bucket {target}-gcs --run {cfg.run}"
        f" --public-keys {shlex.quote(str(keys))}"
        f" --receipt {shlex.quote(str(receipts / 'receipt-aws.json'))}",
        f"{cli} verify-aws --target-bucket {target}-gcs --run {cfg.run}"
        f" --receipt {shlex.quote(str(receipts / 'receipt-aws.json'))}"
        f" --public-keys {shlex.quote(str(keys))}",
        "# 실행 전 확인: logging copy 대상 버킷(<COPY_OUT>)과 필요한 권한.",
        f"gcloud logging copy _Default storage.googleapis.com/<COPY_OUT>"
        f" --location=global --project {cfg.gcp_project}",
        f"{cli} gcp-to-aws --source-bucket <COPY_OUT> --source-prefix ''"
        f" --target-bucket {target}-s3 --run {cfg.run}"
        f" --receipt {shlex.quote(str(receipts / 'receipt-gcp.json'))}",
        f"{cli} verify-gcp --target-bucket {target}-s3 --run {cfg.run}"
        f" --receipt {shlex.quote(str(receipts / 'receipt-gcp.json'))}",
    ]


def compose_env(
    cfg: config.RehearsalConfig, paths: config.StatePaths
) -> dict[str, str]:
    """정리(stop·down)용 compose env. 형식 검사만 통과시키는 자리값이다.

    compose 파일은 비밀 env 를 필수(`:?`)로 받는다. stop·down 은 그 값을 쓰지
    않으므로 실제 비밀번호 대신 자리값을 넣는다.
    """
    unused = "unused-for-down"
    return {
        "SESSION": cfg.run,
        "SAMPLE_APP_IMAGE": cfg.source_image,
        "INGRESS_ENV_FILE": str(paths.job("forward").out / "ingress.env"),
        "INGRESS_CONTROL_TOKEN": unused,
        "AWS_CDC_PASSWORD": unused,
        "GCP_SINK_PASSWORD": unused,
    }


def _shell_setup(
    cfg: config.RehearsalConfig, paths: config.StatePaths, job: config.Job
) -> list[str]:
    state = shlex.quote(str(paths.root))
    lines = [
        f"# {job} job — 한 셸에서 위에서부터 차례로 돌린다.",
        f"cd {shlex.quote(str(CROSSCLOUD_DIR))} && . .venv/bin/activate",
    ]
    if job == "forward":
        lines += [
            f"export SESSION={cfg.run}"
            f" SAMPLE_APP_IMAGE={shlex.quote(cfg.source_image)}",
            "export INGRESS_ENV_FILE="
            + shlex.quote(str(paths.job("forward").out / "ingress.env")),
            'export INGRESS_CONTROL_TOKEN="$(openssl rand -hex 32)"',
        ]
    else:
        lines.append("kill %1  # 순방향 serve-adapter 를 멈춘다(같은 셸)")
    lines.append(
        f'eval "$(python -m crosscloud.rehearsal --state-dir {state}'
        f' env {job})"'
    )
    return lines


def _start_ingress_side(job: config.Job) -> list[str]:
    if job == "forward":
        return [
            "# 접수 큐·Redis 와 Kafka·Connect 를 띄운다.",
            _compose(INGRESS_COMPOSE) + " up -d redis durable-ingress",
            _compose(CONNECT_COMPOSE) + " up -d --build",
        ]
    return [
        "# 접수 큐는 그대로 두고 Connect 만 귀환 비밀번호 env 로 다시 띄운다.",
        _compose(CONNECT_COMPOSE, CONNECT_RETURN_COMPOSE) + " up -d --build",
    ]


def _compose(*files: pathlib.Path) -> str:
    return "docker compose " + " ".join(
        f"-f {shlex.quote(str(path))}" for path in files
    )


def _crosscloud(
    command: str, files: config.JobPaths, *, control: bool, state: bool
) -> str:
    out = files.out
    parts = [
        "crosscloud",
        command,
        f"--cloud-config {shlex.quote(str(out / 'config.cloud.yaml'))}",
        f"--ingress-env {shlex.quote(str(out / 'ingress.env'))}",
    ]
    if control:
        parts.append(f"--ingress-control-url {wiring.INGRESS_CONTROL_URL}")
    if state:
        parts.append(f"--state-dir {shlex.quote(str(files.migration_state))}")
    return " ".join(parts)


def _migration(files: config.JobPaths, action: str) -> str:
    config_file = shlex.quote(str(files.out / "config.cloud.yaml"))
    state = shlex.quote(str(files.migration_state))
    return (
        f"(cd {shlex.quote(str(MIGRATION_DIR))} && python -m migration.cloud"
        f" --config {config_file} --state-dir {state} {action})"
    )
