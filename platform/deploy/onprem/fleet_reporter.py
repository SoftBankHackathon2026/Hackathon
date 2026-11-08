"""cutover 상태·정책 판정·verify 보고서를 플랫폼으로 보내고, 선택적으로 전환 명령을 실행한다.

표준 라이브러리만 쓴다. 플랫폼은 클러스터·DB 에 쓰지 않으므로 명령은 pull 방식이다:
`--execute-commands` 를 켜면 러너 Pod 안에서 이 보고기가 명령을 가져가(claim)
`python -m migration.cutover ...` 서브프로세스를 실행하고 종료 코드와 로그 끝부분을 올린다.
한 번에 한 명령만 실행하고, 비밀값(password/token)은 로그에서 가린다.
`--all-jobs` 를 쓰면 작업 하나에 묶이지 않는다. 플랫폼에 등록된 모든 작업의 명령을 오래된 순서로 하나씩 받아,
기본 설정(MIGRATION_CONFIG)의 targets 는 그대로 두고 job 절만 그 작업의 원본·대상으로 바꾼 설정을
상태 디렉터리 jobs/<job_id>.yaml 에 만들어 실행한다. 상태 파일에 기록이 있는 작업은 모두 보고한다.
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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

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
POLICY_DIR = "policy"
POLICY_EVIDENCE = "CUTOVER_POLICY_EVIDENCE"
POLICY_SETTINGS = "CUTOVER_POLICY_COLLECTOR_SETTINGS"
WRITER_DONE = frozenset({"COMPLETED", "ROLLED_BACK", "ABORTED", "NEEDS_RECOVERY"})
BOT_METRIC = re.compile(
    r'^traffic_bot_(?:requests_total\{state="(?P<state>\w+)"\}'
    r'|commit_latency_ms\{quantile="(?P<quantile>0\.95)"\})\s+(?P<value>\S+)$'
)


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


def get_text(url: str, token: str | None = None) -> str:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode()


def bot_metrics(text: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for line in text.splitlines():
        match = BOT_METRIC.match(line.strip())
        if match:
            found[match["state"] or "latency_p95"] = float(match["value"])
    return found


def writer_report(config_path: Path) -> dict | None:
    import yaml

    config = yaml.safe_load(config_path.read_text()) or {}
    bot, ingress = config.get("bot") or {}, config.get("ingress") or {}
    if not bot.get("control_url") or not ingress.get("control_url"):
        return None
    metrics = bot_metrics(get_text(bot["control_url"].rstrip("/") + "/metrics"))
    token = os.environ.get(ingress.get("token_env") or "INGRESS_CONTROL_TOKEN")
    status = json.loads(get_text(ingress["control_url"].rstrip("/") + "/status", token))
    names = {ingress_name: name for name, ingress_name in (ingress.get("targets") or {}).items()}
    return {
        "target": names.get(status.get("target"), ""),
        "mode": str(status.get("mode") or ""),
        "committed": int(metrics.get("committed", 0)),
        "failed": int(metrics.get("failed", 0)),
        "pending": int(metrics.get("pending", 0)),
        "latency_p95_ms": metrics.get("latency_p95"),
    }


def job_writer(state_dir: Path, job_id: str, cutover: dict | None) -> dict | None:
    config_path = state_dir / "jobs" / f"{job_id}.yaml"
    if cutover is None or cutover["stage"] in WRITER_DONE or not config_path.exists():
        return None
    try:
        return writer_report(config_path)
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        log(f"writer status for {job_id} failed: {exc}")
        return None


def report_job(args: argparse.Namespace, token: str, job_id: str) -> None:
    state_dir = Path(args.state_dir)
    url = f"{args.platform_url.rstrip('/')}/api/v1/ingest/migrations/{job_id}"
    cutover = read_cutover(state_dir, job_id)
    body = {"cutover": cutover, "policy_decisions": read_decisions(state_dir, job_id)}
    writer = job_writer(state_dir, job_id, cutover)
    if writer:
        body["writer"] = writer
    post(url, token, body)
    for verify in read_verify(Path(args.report_dir or state_dir / "reports"), job_id):
        post(url, token, {"verify": verify})


def report_once(args: argparse.Namespace, token: str) -> None:
    report_job(args, token, args.job_id)


def known_jobs(state_dir: Path) -> list[str]:
    rows = read_rows(state_dir / CUTOVER_DB, "SELECT job_id FROM cutover_jobs ORDER BY job_id", ())
    return [row["job_id"] for row in rows]


def report_all(args: argparse.Namespace, token: str) -> None:
    for job_id in known_jobs(Path(args.state_dir)):
        try:
            report_job(args, token, job_id)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise


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
    argv: list[str],
    on_tick: Callable[[], None] | None = None,
    tick_seconds: float = 5.0,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """cutover 서브프로세스를 끝까지 돌린다. 실행 중에도 tick_seconds 마다 on_tick(보통 report_once)을
    불러 플랫폼 화면의 단계가 멈추지 않게 한다. 출력은 가린 뒤 끝 LOG_TAIL_LIMIT 자만 남긴다."""
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=env
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


def commands_url(args: argparse.Namespace, suffix: str, job_id: str | None = None) -> str:
    job = job_id or args.job_id
    return f"{args.platform_url.rstrip('/')}/api/v1/ingest/migrations/{job}/commands/{suffix}"


def claim_command(args: argparse.Namespace, token: str) -> dict | None:
    status, body = post_json(commands_url(args, "claim"), token, {})
    return body if status == 200 and body else None


def post_result(
    args: argparse.Namespace,
    token: str,
    command_id: str,
    result: dict,
    job_id: str | None = None,
) -> None:
    url = commands_url(args, f"{command_id}/result", job_id)
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


def claim_any_command(args: argparse.Namespace, token: str) -> dict | None:
    url = f"{args.platform_url.rstrip('/')}/api/v1/ingest/commands/claim"
    status, body = post_json(url, token, {})
    return body if status == 200 and body else None


def job_config(base_path: str, claimed: dict, out_dir: Path) -> Path:
    import yaml

    base = yaml.safe_load(Path(base_path).read_text()) or {}
    targets = base.get("targets") or {}
    source, target = claimed["data_source"], claimed["data_target"]
    if base.get("full_migration") is not None:
        mapping = base.get("platform_data_targets")
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("full migration requires explicit platform_data_targets")
        resolved = []
        for side in ("source", "target"):
            environment = claimed.get(f"{side}_env")
            configured = mapping.get(environment)
            requested = claimed[f"data_{side}"]
            if not isinstance(configured, str) or requested not in (environment, configured):
                raise ValueError(f"full migration {side} environment/data target mismatch")
            resolved.append(configured)
        source, target = resolved
        base_job = base.get("job") or {}
        if (source, target) != (base_job.get("source"), base_job.get("target")):
            raise ValueError("full migration only supports the configured source/target direction")
        allowed_apps, selected_apps = base.get("platform_apps"), claimed.get("apps")
        if (not isinstance(allowed_apps, list) or not allowed_apps
                or not all(isinstance(app, str) and app for app in allowed_apps)
                or not isinstance(selected_apps, list) or len(selected_apps) != 1
                or selected_apps[0] not in allowed_apps):
            raise ValueError("full migration requires exactly one configured sample-app selection")
    else:
        aliases = base.get("platform_data_targets") or {}
        source, target = (
            aliases.get(name, name) if name == claimed.get(f"{side}_env") else name
            for side, name in (("source", source), ("target", target))
        )
    missing = [name for name in (source, target) if name not in targets]
    if missing:
        raise ValueError(f"러너 설정 targets 에 없는 대상입니다: {', '.join(missing)}")
    job_id = claimed["job_id"]
    base["job"] = {
        "job_id": job_id,
        "source": source,
        "target": target,
        "table": (base.get("job") or {}).get("table", "public.migration_demo"),
        "live_phase_before": f"{source}_live",
        "live_phase_after": f"{target}_live",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{job_id}.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False))
    path.chmod(0o600)
    return path


def policy_env(claimed: dict, state_dir: Path) -> dict[str, str]:
    job_id = claimed["job_id"]
    out_dir = state_dir / POLICY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in (POLICY_EVIDENCE, POLICY_SETTINGS)}
    env[POLICY_EVIDENCE] = str(out_dir / f"{job_id}.json")
    collectors_path = os.environ.get("CUTOVER_POLICY_COLLECTORS")
    if not collectors_path:
        return env
    collectors = json.loads(Path(collectors_path).read_text())
    if not isinstance(collectors, dict):
        raise TypeError("정책 증거 수집 설정은 대상 이름별 객체여야 합니다.")
    settings = collectors.get(claimed["data_target"])
    if settings is None:
        return env
    path = out_dir / f"{job_id}.settings.json"
    path.write_text(json.dumps(settings))
    path.chmod(0o600)
    env[POLICY_SETTINGS] = str(path)
    return env


def execute_any(args: argparse.Namespace, token: str) -> bool:
    claimed = claim_any_command(args, token)
    if claimed is None:
        return False
    command, job_id = claimed["command"], claimed["job_id"]
    command_id = command["id"]
    log(f"command {command_id} claimed: {command.get('action')} job={job_id}")
    state_dir = Path(args.state_dir)
    try:
        config = job_config(os.environ.get("MIGRATION_CONFIG", ""), claimed, state_dir / "jobs")
        argv = command_argv(
            command,
            shlex.split(os.environ.get("ONEPORT_CUTOVER_ARGV") or DEFAULT_CUTOVER_ARGV),
            str(config),
            args.state_dir,
        )
        env = policy_env(claimed, state_dir)
        log(f"command {command_id} argv: {' '.join(argv)}")
        exit_code, tail = run_command(
            argv,
            on_tick=lambda: report_job(args, token, job_id),
            tick_seconds=float(getattr(args, "interval", 0) or 5),
            env=env,
        )
    except (ValueError, TypeError, OSError) as exc:
        exit_code, tail = EXIT_CONFIG, redact(str(exc))
    cutover = read_cutover(state_dir, job_id)
    stage = cutover["stage"] if cutover else None
    post_result(
        args, token, command_id, {"exit_code": exit_code, "stage": stage, "log_tail": tail}, job_id
    )
    log(f"command {command_id} finished exit={exit_code} stage={stage} job={job_id}")
    try:
        report_job(args, token, job_id)
    except (urllib.error.URLError, OSError) as exc:
        log(f"report after command failed: {exc}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="cutover 상태·정책 판정·verify 보고서를 플랫폼으로 보낸다"
    )
    jobs = parser.add_mutually_exclusive_group(required=True)
    jobs.add_argument("--job-id")
    jobs.add_argument(
        "--all-jobs",
        action="store_true",
        help="플랫폼에 등록된 모든 작업의 명령을 순서대로 받아 실행하고, 작업마다 상태를 보고한다",
    )
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
        # 플랫폼이 잠시 죽거나(502·연결 거부) 느려도 보고기는 내려가지 않는다. 다음 주기에 다시 보낸다.
        reported = True
        try:
            report_all(args, token) if args.all_jobs else report_once(args, token)
        except (urllib.error.URLError, OSError) as exc:
            reported = False
            log(f"report failed: {exc}")
        if args.execute_commands:
            try:
                if args.all_jobs:
                    execute_any(args, token)
                elif execute_once(args, token):
                    report_once(args, token)  # 실행 직후 바뀐 단계를 바로 올린다
            except (urllib.error.URLError, OSError) as exc:
                log(f"command step failed: {exc}")
        if args.interval <= 0:
            return 0 if reported else 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
