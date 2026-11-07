"""cutover 상태·정책 판정·verify 보고서를 플랫폼으로 보내고, 선택적으로 전환 명령을 실행한다.

표준 라이브러리만 쓴다. 플랫폼은 클러스터·DB 에 쓰지 않으므로 명령은 pull 방식이다:
`--execute-commands` 를 켜면 러너 Pod 안에서 이 보고기가 명령을 가져가(claim)
`python -m migration.cutover ...` 서브프로세스를 실행하고 종료 코드와 로그 끝부분을 올린다.
한 번에 한 명령만 실행하고, 비밀값(password/token)은 로그에서 가린다.
"""

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

CUTOVER_DB = "cutover.sqlite3"
POLICY_DB = "policy.sqlite3"
VERIFY_LABELS = {"pre": ("post-stop", "pre-stop"), "post": ("final",)}
DEFAULT_CUTOVER_ARGV = "python -m migration.cutover"
LOG_TAIL_LIMIT = 4000
RESULT_POST_ATTEMPTS = 3
SECRET_PATTERN = re.compile(r'(password|PASSWORD|token)(["=: ]+)\S+', re.IGNORECASE)
ACTION_ARGS = {
    "run": ["run"],
    "approve": ["run", "--approve"],
    "retry": ["run", "--retry"],
    "force_retry": ["run", "--force-retry"],
    "abort": ["abort"],
}
EXIT_CONFIG = 2


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat(timespec='seconds')}] {message}", flush=True)


def read_rows(path: Path, query: str, params: tuple) -> list[dict]:
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, params)]


def read_cutover(state_dir: Path, job_id: str) -> dict | None:
    rows = read_rows(
        state_dir / CUTOVER_DB,
        "SELECT job_id, stage, checkpoint, updated_at FROM cutover_jobs WHERE job_id = ?",
        (job_id,),
    )
    if not rows:
        return None
    row = rows[0]
    return {**row, "checkpoint": json.loads(row["checkpoint"])}


def read_decisions(state_dir: Path, job_id: str) -> list[dict]:
    rows = read_rows(
        state_dir / POLICY_DB,
        "SELECT decision_id, job_id, stage, outcome, reason_codes, policy_version, evaluated_at "
        "FROM policy_decisions WHERE job_id = ? ORDER BY evaluated_at",
        (job_id,),
    )
    return [{**row, "reason_codes": json.loads(row["reason_codes"])} for row in rows]


def read_verify(report_dir: Path, job_id: str) -> list[dict]:
    reports = []
    for mode, labels in VERIFY_LABELS.items():
        for label in labels:
            path = report_dir / f"verify-{job_id}-{label}.json"
            if path.exists():
                reports.append({"mode": mode, "report": json.loads(path.read_text())})
                break
    return reports


def post_json(url: str, token: str, body: dict) -> tuple[int, dict | None]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)


def post(url: str, token: str, body: dict) -> int:
    return post_json(url, token, body)[0]


def report_once(args: argparse.Namespace, token: str) -> None:
    state_dir = Path(args.state_dir)
    url = f"{args.platform_url.rstrip('/')}/api/v1/ingest/migrations/{args.job_id}"
    body = {
        "cutover": read_cutover(state_dir, args.job_id),
        "policy_decisions": read_decisions(state_dir, args.job_id),
    }
    post(url, token, body)
    for verify in read_verify(Path(args.report_dir or state_dir / "reports"), args.job_id):
        post(url, token, {"verify": verify})


# ── 전환 명령 실행 ───────────────────────────────────────────────


def redact(text: str) -> str:
    return SECRET_PATTERN.sub(r"\1\2[redacted]", text)


def command_argv(
    command: dict, base_argv: list[str], config: str | None, state_dir: str
) -> list[str]:
    action = command.get("action")
    if action not in ACTION_ARGS:
        raise ValueError(f"지원하지 않는 명령입니다: {action}")
    argv = list(base_argv)
    if config:
        argv += ["--config", config]
    argv += ["--state-dir", state_dir, *ACTION_ARGS[action]]
    if action == "approve":
        approval_id = (command.get("approval_id") or "").strip()
        if not approval_id:
            raise ValueError("approve 명령에 approval_id 가 없습니다.")
        argv.append(approval_id)
    return argv


def run_command(
    argv: list[str], on_tick: Callable[[], None] | None = None, tick_seconds: float = 5.0
) -> tuple[int, str]:
    """cutover 서브프로세스를 끝까지 돌린다. 실행 중에도 tick_seconds 마다 on_tick(보통 report_once)을
    불러 플랫폼 화면의 단계가 멈추지 않게 한다. 출력은 가린 뒤 끝 LOG_TAIL_LIMIT 자만 남긴다."""
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace"
    )
    chunks: list[str] = []

    def drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            if sum(len(c) for c in chunks) > LOG_TAIL_LIMIT * 4:
                joined = "".join(chunks)[-LOG_TAIL_LIMIT * 2 :]
                chunks.clear()
                chunks.append(joined)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    while proc.poll() is None:
        reader.join(timeout=max(0.2, tick_seconds))
        if proc.poll() is None and on_tick is not None:
            try:
                on_tick()
            except (urllib.error.URLError, OSError) as exc:
                log(f"report during command failed: {exc}")
    reader.join()
    return proc.returncode, redact("".join(chunks))[-LOG_TAIL_LIMIT:]


def commands_url(args: argparse.Namespace, suffix: str) -> str:
    return (
        f"{args.platform_url.rstrip('/')}/api/v1/ingest/migrations/{args.job_id}/commands/{suffix}"
    )


def claim_command(args: argparse.Namespace, token: str) -> dict | None:
    status, body = post_json(commands_url(args, "claim"), token, {})
    return body if status == 200 and body else None


def post_result(args: argparse.Namespace, token: str, command_id: str, result: dict) -> None:
    url = commands_url(args, f"{command_id}/result")
    for attempt in range(1, RESULT_POST_ATTEMPTS + 1):
        try:
            post_json(url, token, result)
            return
        except (urllib.error.URLError, OSError) as exc:
            log(
                f"command {command_id} result post failed ({attempt}/{RESULT_POST_ATTEMPTS}): {exc}"
            )
            if attempt == RESULT_POST_ATTEMPTS:
                raise
            time.sleep(2)


def execute_once(args: argparse.Namespace, token: str) -> bool:
    """대기 중인 명령 하나를 가져가 실행하고 결과를 올린다. 실행한 것이 있으면 True."""
    command = claim_command(args, token)
    if command is None:
        return False
    command_id = command["id"]
    log(f"command {command_id} claimed: {command.get('action')} job={args.job_id}")
    try:
        argv = command_argv(
            command,
            shlex.split(os.environ.get("ONEPORT_CUTOVER_ARGV") or DEFAULT_CUTOVER_ARGV),
            os.environ.get("MIGRATION_CONFIG"),
            args.state_dir,
        )
        log(f"command {command_id} argv: {' '.join(argv)}")
        exit_code, tail = run_command(
            argv,
            on_tick=lambda: report_once(args, token),
            tick_seconds=float(getattr(args, "interval", 0) or 5),
        )
    except (ValueError, OSError) as exc:
        exit_code, tail = EXIT_CONFIG, redact(str(exc))
    cutover = read_cutover(Path(args.state_dir), args.job_id)
    stage = cutover["stage"] if cutover else None
    post_result(args, token, command_id, {"exit_code": exit_code, "stage": stage, "log_tail": tail})
    log(f"command {command_id} finished exit={exit_code} stage={stage}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="cutover 상태·정책 판정·verify 보고서를 플랫폼으로 보낸다"
    )
    parser.add_argument("--job-id", required=True)
    parser.add_argument(
        "--state-dir", default=os.environ.get("WORKER_STATE_DIR", "/var/lib/worker")
    )
    parser.add_argument("--report-dir")
    parser.add_argument("--platform-url", default=os.environ.get("ONEPORT_PLATFORM_URL"))
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument(
        "--execute-commands",
        action="store_true",
        help="플랫폼의 전환 명령을 가져가 cutover 서브프로세스를 실행한다(러너 Pod 전용)",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("ONEPORT_INGEST_TOKEN")
    if not args.platform_url or not token:
        parser.error("ONEPORT_PLATFORM_URL과 ONEPORT_INGEST_TOKEN이 필요합니다.")
    while True:
        report_once(args, token)
        if args.execute_commands:
            try:
                if execute_once(args, token):
                    report_once(args, token)  # 실행 직후 바뀐 단계를 바로 올린다
            except (urllib.error.URLError, OSError) as exc:
                log(f"command step failed: {exc}")
        if args.interval <= 0:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
