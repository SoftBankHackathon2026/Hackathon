"""Operator-only commands (same shape as kubernetes_operator), infra Notion "플랫폼 개발 범위".

배포
  preflight  --config runtime.json                         읽기 전용 사전 검사
  register   --config runtime.json --port 8080 --health-path /healthz [--database postgresql]
             [--database-mode shared|dedicated] [--redis]
                                                           앱 등록: 포트·health·도메인·rule priority·DB 선택.
                                                           DB를 고르면 worker가 배포와 별개로 RDS를 준비
  build      --config runtime.json --request git.json [--out image.json]
                                                           Git 커밋 -> oneport/<app>:<sha> push -> digest 요청 출력
  enqueue    --config runtime.json --request req.json --key K [--wait-db]
                                                           Git 소스면 먼저 build 후 digest로 접수.
                                                           DB 앱은 DB가 준비돼야 접수 (--wait-db: 준비될 때까지 진행·대기)
  worker     --config runtime.json [--once]                접수된 Release 실행
  show       --config runtime.json --release ID            Release 단계·이벤트
  restore    --config runtime.json --release ID --key K    bake 이후 롤백: 이전 성공 Release의 revision으로 update_service
  delete     --config runtime.json --yes                   service/rule/TG/TD/알람/runtime role 삭제
운영
  status     --config runtime.json                         지금 실행 상태 (AWS 조회) + 앱 등록·DB 상태
  secret-put --config runtime.json --binding NAME          값은 stdin으로. Version 고정 참조를 플랫폼 DB에 기록
  db-create  --config runtime.json [--wait]                등록된 앱에 DB 선택 추가(요청). --wait: 준비될 때까지 진행
  db-delete  --config runtime.json --yes                   DB 삭제 (최종 스냅샷 남김). 앱 삭제와는 별개
  cache-delete --config runtime.json --yes                 캐시·user group·사용자 삭제 (약 4분 대기). 앱 삭제와는 별개
  logs       --config runtime.json [--minutes N] [--container C] [--task ID] [--grep TEXT] [--follow S]
"""

import argparse
from pathlib import Path

import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from controlplane.core.errors import RequestConflict
from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
)
from oneport_aws.execution.oneport_ecs_commands import COMMANDS, run_command
from oneport_aws.execution.oneport_ecs_runtime import OneportEcsSettings
from oneport_aws.execution.oneport_ecs_session import OperatorSession, UsageError, print_json
from oneport_aws.providers.oneport_ecs.registry import BuildFailed

OWN_ERRORS = (
    ProviderConflict,
    ProviderRejected,
    ProviderUnavailable,
    SubmissionUncertain,
    RequestConflict,
)
HANDLED_ERRORS = (
    ValidationError,
    ValueError,
    TypeError,
    OSError,
    KeyError,
    TimeoutError,
    psycopg.Error,
    ClientError,
    BotoCoreError,
    *OWN_ERRORS,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--key")
    parser.add_argument("--release")
    parser.add_argument("--binding")
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--container")
    parser.add_argument("--task")
    parser.add_argument("--grep")
    parser.add_argument("--follow", type=int, metavar="SECONDS")
    parser.add_argument("--port", type=int)
    parser.add_argument("--health-path")
    parser.add_argument("--database", choices=["none", "postgresql"], default="none")
    parser.add_argument("--database-mode", choices=["shared", "dedicated"], default="dedicated")
    parser.add_argument("--redis", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-db", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--yes", action="store_true")
    return parser


def error_message(exc: Exception) -> str:
    if isinstance(exc, OWN_ERRORS):
        return str(exc)
    return f"{type(exc).__name__}: check operator inputs and AWS permissions"


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    session: OperatorSession | None = None
    try:
        settings = OneportEcsSettings.model_validate_json(args.config.read_text())
        session = OperatorSession(args, settings)
        return run_command(session)
    except UsageError as exc:
        parser.error(str(exc))
    except BuildFailed as exc:
        print_json({"error": str(exc), "build_output_tail": exc.detail})
        return 1
    except HANDLED_ERRORS as exc:
        print_json({"error": error_message(exc)})
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if session:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
