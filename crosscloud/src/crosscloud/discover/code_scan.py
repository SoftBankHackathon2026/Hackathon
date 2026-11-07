"""앱 소스에서 인프라 결합 지점(InfraCoupling)을 찾는다.

결합은 앱 코드가 클라우드 인증·SDK 를 직접 부르는 자리다(설계 4.4). env·Secret
에서 접속 정보를 읽기만 하는 코드는 결합이 아니다. 본선은 DB IAM 인증 표식만
줄 단위 정규식으로 찾는다(Semgrep 대신 — 계획 R1 단순화).

  couplings = code_scan.scan_couplings("request-api", pathlib.Path("app"))
"""

import dataclasses
import pathlib
import re

from crosscloud import model


@dataclasses.dataclass(frozen=True)
class _Rule:
    rule_id: str
    pattern: re.Pattern[str]
    category: model.CouplingCategory
    certainty: model.Certainty


_RULES = (
    _Rule(
        "aws-rds-signer-import",
        re.compile(r"@aws-sdk/rds-signer"),
        "db_auth_rds_iam",
        "mandatory",
    ),
    _Rule(
        "aws-rds-signer-new",
        re.compile(r"\bnew\s+Signer\s*\("),
        "db_auth_rds_iam",
        "mandatory",
    ),
    _Rule(
        "aws-rds-generate-auth-token",
        re.compile(r"\bgenerate_db_auth_token\s*\("),
        "db_auth_rds_iam",
        "mandatory",
    ),
    # 인증 방식을 env 로 고르는 스위치. 어느 방식인지 코드만으로는 모른다.
    _Rule(
        "db-auth-mode-switch",
        re.compile(r"\bDB_AUTH_MODE\b"),
        "db_auth_rds_iam",
        "potential",
    ),
    # 커넥터는 비밀번호 인증에도 쓰므로 IAM 표식이 따로 있어야 확정이다.
    _Rule(
        "gcp-cloudsql-connector",
        re.compile(
            r"@google-cloud/cloud-sql-connector"
            r"|google\.cloud\.sql\.connector"
            r"|cloud-sql-python-connector"
        ),
        "db_auth_cloudsql_iam",
        "potential",
    ),
    _Rule(
        "gcp-cloudsql-iam-auth",
        re.compile(
            r"AuthTypes\.IAM"
            r"|authType\s*:\s*['\"]IAM['\"]"
            r"|enable_iam_auth\s*=\s*True"
        ),
        "db_auth_cloudsql_iam",
        "mandatory",
    ),
)

_SOURCE_SUFFIXES = frozenset(
    {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py", ".go", ".java", ".kt"}
)
_MANIFEST_NAMES = frozenset(
    {"package.json", "requirements.txt", "pyproject.toml", "go.mod"}
)
_SKIPPED_DIRS = frozenset(
    {".git", "node_modules", "vendor", ".venv", "dist", "build", "__pycache__"}
)
# 이보다 큰 파일은 번들·생성물로 보고 읽지 않는다.
_MAX_FILE_BYTES = 1024 * 1024


def scan_couplings(
    workload: str, repo_dir: pathlib.Path
) -> tuple[model.InfraCoupling, ...]:
    """소스 디렉토리에서 결합 지점을 찾는다.

    Args:
        workload: 결과에 적을 워크로드 이름(Workload.name).
        repo_dir: 앱 소스 체크아웃 경로.

    Returns:
        파일 경로·줄 번호 순의 결합 지점. 없으면 빈 tuple(결합 0건).

    Raises:
        FileNotFoundError: repo_dir 가 디렉토리가 아닐 때. 소스를 못 본 것을
            결합 0건으로 보고하지 않기 위해서다.
    """
    if not repo_dir.is_dir():
        raise FileNotFoundError(f"소스 디렉토리가 없다: {repo_dir}")
    return tuple(
        coupling
        for path in _source_files(repo_dir)
        for coupling in _scan_file(workload, repo_dir, path)
    )


def _source_files(repo_dir: pathlib.Path) -> list[pathlib.Path]:
    files = []
    for path in sorted(repo_dir.rglob("*")):
        relative_parts = path.relative_to(repo_dir).parts
        if _SKIPPED_DIRS.intersection(relative_parts[:-1]):
            continue
        if not path.is_file():
            continue
        if path.suffix in _SOURCE_SUFFIXES or path.name in _MANIFEST_NAMES:
            files.append(path)
    return files


def _scan_file(
    workload: str, repo_dir: pathlib.Path, path: pathlib.Path
) -> list[model.InfraCoupling]:
    if path.stat().st_size > _MAX_FILE_BYTES:
        return []
    relative = path.relative_to(repo_dir).as_posix()
    text = path.read_text(encoding="utf-8", errors="replace")
    return [
        model.InfraCoupling(
            workload=workload,
            location=f"{relative}:{line_number}",
            rule_id=rule.rule_id,
            category=rule.category,
            certainty=rule.certainty,
        )
        for line_number, line in enumerate(text.splitlines(), start=1)
        for rule in _RULES
        if rule.pattern.search(line)
    ]
