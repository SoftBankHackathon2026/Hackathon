"""리허설 설정과 상태 디렉터리 배치. 비밀값은 담지 않는다."""

import dataclasses
import json
import pathlib
import re
from typing import Literal

Job = Literal["forward", "return"]
JOBS: tuple[Job, ...] = ("forward", "return")
# 귀환 스택 접두사 ccx-<run>c 도 residue.NAME_PREFIX_PATTERN(1–16자)에 든다.
_RUN = re.compile(r"[a-z0-9]{1,15}")
_DIGEST_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")


@dataclasses.dataclass(frozen=True)
class RehearsalConfig:
    """한 번의 왕복 리허설 설정.

    Attributes:
        run: 실행 이름. 자원 접두사 ccx-<run>, compose 세션 이름이 된다.
        source_image: 팀 CI 이미지(digest 고정). 우리 레지스트리로 복사만 한다.
        aws_account: 기대하는 AWS 계정(사전 점검이 대조).
        aws_region: AWS 리전.
        gcp_project: GCP 프로젝트.
        gcp_region: GCP 리전.
        source_database: 원본(AWS RDS) DB 이름.
        forward_database: 순방향 대상(Cloud SQL) DB 이름.
        return_database: 귀환 대상(새 RDS) DB 이름.
        app_user: 앱 DB 로그인.
        table: 옮길 표(소문자 schema.table).
        primary_key: 그 표의 기본키 열.
        seed_rows: 원본에 넣을 시험 행 수.
    """

    run: str
    source_image: str
    aws_account: str = "187478112557"
    aws_region: str = "ap-northeast-2"
    gcp_project: str = "ghost-project-513812"
    gcp_region: str = "asia-northeast3"
    source_database: str = "shop"
    forward_database: str = "shop_b"
    return_database: str = "shop_c"
    app_user: str = "shop_app"
    table: str = "public.migration_demo"
    primary_key: str = "request_id"
    seed_rows: int = 20

    def __post_init__(self) -> None:
        """이름 규칙을 검사한다.

        Raises:
            ValueError: run 이 영문 소문자·숫자 1–15자가 아니거나, 이미지가
                digest 로 고정되지 않았을 때.
        """
        if not _RUN.fullmatch(self.run):
            raise ValueError("run 은 영문 소문자·숫자 1–15자여야 한다")
        if not _DIGEST_IMAGE.fullmatch(self.source_image):
            raise ValueError(
                "source_image 는 <저장소>@sha256:<digest> 여야 한다"
            )

    @property
    def prefix(self) -> str:
        """첫 AWS 스택과 GCP 자원의 접두사."""
        return f"ccx-{self.run}"

    @property
    def return_prefix(self) -> str:
        """귀환 대상 AWS 스택(새 RDS)의 접두사."""
        return f"ccx-{self.run}c"


def save(cfg: RehearsalConfig, path: pathlib.Path) -> None:
    """설정을 JSON 으로 쓴다."""
    path.write_text(json.dumps(dataclasses.asdict(cfg), indent=2) + "\n")


def load(path: pathlib.Path) -> RehearsalConfig:
    """Save 가 쓴 설정을 읽는다."""
    return RehearsalConfig(**json.loads(path.read_text()))


@dataclasses.dataclass(frozen=True)
class JobPaths:
    """한 job(순방향·귀환)의 인계·전환 파일 자리."""

    root: pathlib.Path

    @property
    def discover(self) -> pathlib.Path:
        """Crosscloud discover 결과."""
        return self.root / "discover.json"

    @property
    def source_tf(self) -> pathlib.Path:
        """render-handoff --source-tf."""
        return self.root / "source-tf.json"

    @property
    def target_tf(self) -> pathlib.Path:
        """render-handoff --target-tf."""
        return self.root / "target-tf.json"

    @property
    def readiness(self) -> pathlib.Path:
        """render-handoff --readiness."""
        return self.root / "readiness.json"

    @property
    def deploy(self) -> pathlib.Path:
        """prepare-target --deploy."""
        return self.root / "target-deploy.yaml"

    @property
    def out(self) -> pathlib.Path:
        """render-handoff --out-dir(config.cloud.yaml·ingress.env)."""
        return self.root / "out"

    @property
    def migration_state(self) -> pathlib.Path:
        """원가연 migration.cloud --state-dir."""
        return self.root / "migration-state"


@dataclasses.dataclass(frozen=True)
class StatePaths:
    """상태 디렉터리 배치. 단계를 멈춘 곳에서 이어 가는 근거다."""

    root: pathlib.Path

    @property
    def config(self) -> pathlib.Path:
        """리허설 설정."""
        return self.root / "config.json"

    @property
    def rds_ca(self) -> pathlib.Path:
        """RDS 리전 CA 묶음(공개 인증서)."""
        return self.root / "rds-ca.pem"

    @property
    def db_ca(self) -> pathlib.Path:
        """RDS 묶음 + Cloud SQL 서버 CA 합본(앱 DB_SSL_CA)."""
        return self.root / "db-ca.pem"

    @property
    def values(self) -> pathlib.Path:
        """단계가 다음 단계에 넘기는 비밀 아닌 값(이미지 ref·버전 번호)."""
        return self.root / "values.json"

    def tfvars(self, stack: str) -> pathlib.Path:
        """스택의 변수 파일."""
        return self.root / "tf" / f"{stack}.tfvars.json"

    def tfstate(self, stack: str) -> pathlib.Path:
        """스택의 state 파일."""
        return self.root / "tf" / f"{stack}.tfstate"

    def outputs(self, stack: str) -> pathlib.Path:
        """스택의 출력(sensitive 제외 이름 → 값)."""
        return self.root / "tf" / f"{stack}.outputs.json"

    def done(self, step: str) -> pathlib.Path:
        """단계 완료 표시."""
        return self.root / "done" / step

    def job(self, job: Job) -> JobPaths:
        """Job 의 파일 자리."""
        return JobPaths(self.root / job)
