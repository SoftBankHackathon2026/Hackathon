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


class NoSourceFilesError(ValueError):
    """소스 디렉토리에 검사할 소스 파일이 하나도 없다.

    검사하지 못한 것을 결합 0건으로 보고하지 않기 위해 올린다.
    """


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

_JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"})
_SOURCE_SUFFIXES = _JS_SUFFIXES | {".py", ".go", ".java", ".kt"}
# 의존성 매니페스트와 그 매니페스트가 가리키는 주 언어의 소스 확장자.
_MANIFEST_LANGUAGES = {
    "package.json": _JS_SUFFIXES,
    "requirements.txt": frozenset({".py"}),
    "pyproject.toml": frozenset({".py"}),
    "go.mod": frozenset({".go"}),
}
_SKIPPED_DIRS = frozenset(
    {".git", "node_modules", "vendor", ".venv", "dist", "build", "__pycache__"}
)
# 이보다 큰 파일은 번들·생성물로 보고 읽지 않는다. 건너뛴 파일은 보고한다.
_MAX_FILE_BYTES = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class SourceScan:
    """소스 검사 결과.

    Attributes:
        couplings: 파일 경로·줄 번호 순의 결합 지점. 없으면 결합 0건.
        skipped_large_files: 크기 제한으로 읽지 않은 파일(저장소 기준 상대
            경로). 하나라도 있으면 결합 0건을 다 믿을 수는 없다.
    """

    couplings: tuple[model.InfraCoupling, ...]
    skipped_large_files: tuple[str, ...]


def scan_couplings(workload: str, repo_dir: pathlib.Path) -> SourceScan:
    """소스 디렉토리에서 결합 지점을 찾는다.

    "검사했다"는 앱 소스 언어 파일을 하나 이상 읽었다는 뜻이다. 매니페스트만
    읽었거나, 매니페스트가 가리키는 주 언어 파일을 하나도 못 읽었으면 검사하지
    않은 것으로 본다.

    Args:
        workload: 결과에 적을 워크로드 이름(Workload.name).
        repo_dir: 앱 소스 체크아웃 경로.

    Returns:
        결합 지점과 건너뛴 큰 파일.

    Raises:
        FileNotFoundError: repo_dir 가 디렉토리가 아닐 때. 소스를 못 본 것을
            결합 0건으로 보고하지 않기 위해서다.
        NoSourceFilesError: 앱 소스 언어 파일을 하나도 읽지 못했거나, 주 언어
            파일을 하나도 읽지 못했을 때. 같은 이유다.
    """
    if not repo_dir.is_dir():
        raise FileNotFoundError(f"소스 디렉토리가 없다: {repo_dir}")
    readable, skipped = _candidate_files(repo_dir)
    read_suffixes = {path.suffix for path in readable if _is_source(path)}
    if not read_suffixes:
        raise NoSourceFilesError(
            f"{workload}: 읽은 소스 파일이 없다"
            f"(큰 파일 {len(skipped)}개 건너뜀): {repo_dir}"
        )
    for path in readable:
        languages = _MANIFEST_LANGUAGES.get(path.name)
        if languages is not None and not read_suffixes & languages:
            relative = path.relative_to(repo_dir).as_posix()
            raise NoSourceFilesError(
                f"{workload}: {relative} 의 주 언어 소스를 읽지 못했다"
            )
    return SourceScan(
        couplings=tuple(
            coupling
            for path in readable
            for coupling in _scan_file(workload, repo_dir, path)
        ),
        skipped_large_files=tuple(
            path.relative_to(repo_dir).as_posix() for path in skipped
        ),
    )


def _is_source(path: pathlib.Path) -> bool:
    return path.suffix in _SOURCE_SUFFIXES


def _candidate_files(
    repo_dir: pathlib.Path,
) -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    readable, skipped = [], []
    for path in sorted(repo_dir.rglob("*")):
        relative_parts = path.relative_to(repo_dir).parts
        if _SKIPPED_DIRS.intersection(relative_parts[:-1]):
            continue
        if not path.is_file():
            continue
        if not (_is_source(path) or path.name in _MANIFEST_LANGUAGES):
            continue
        if path.stat().st_size > _MAX_FILE_BYTES:
            skipped.append(path)
        else:
            readable.append(path)
    return readable, skipped


def _scan_file(
    workload: str, repo_dir: pathlib.Path, path: pathlib.Path
) -> list[model.InfraCoupling]:
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
