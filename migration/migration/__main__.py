"""`python -m migration` 기본 진입점: 사용 가능한 명령을 안내한다."""

from __future__ import annotations

import sys

COMMANDS: tuple[tuple[str, str], ...] = (
    ("migration.init_db", "DB 생성, 스키마·역할 적용, 비밀번호 설정 (--target onprem|aws)"),
    ("migration.seed", "seed 1,000건 결정적 삽입 후 건수 확인"),
    ("migration.loadgen", "INSERT 워커 실행 (HTTP 제어 API 8080)"),
    ("migration.cdc", "Kafka Connect 커넥터 준비/등록/상태/정지/삭제"),
    ("migration.cutover", "전환 상태 머신 실행 (--dry-run 지원)"),
    ("migration.verify", "원본 B / 대상 T / 성공 요청 S 집합 비교 보고서"),
    ("migration.policy", "OPA 정책 판정 (evaluate/history/show). 실행 전 gate 와 판정 기록"),
)


def main(argv: list[str] | None = None) -> int:
    """명령 목록을 출력한다."""
    out = sys.stdout
    out.write("migration-tools 사용법: python -m <모듈> --help\n\n")
    for module, desc in COMMANDS:
        out.write(f"  python -m {module:<22} {desc}\n")
    out.write("\n설정 파일 예시: config.example.yaml, 비밀번호는 환경변수로만 전달한다.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
