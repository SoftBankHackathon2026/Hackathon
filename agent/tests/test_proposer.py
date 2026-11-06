"""CodexProposer 를 가짜 codex 실행 파일로 시험한다: 호출 인자, stdin 닫힘, 환경 격리, 스키마 위반."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from agent.config import CodexSettings
from agent.codex_runner import process_pending
from agent.proposer import (
    OUTPUT_SCHEMA,
    CodexProposer,
    ProposalInvalid,
    ProposerError,
    SidecarCodexRunner,
    build_proposer,
    parse_output,
)

CURRENT = {"DB_HOST": "wrong.svc", "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u", "DB_PASSWORD": "must-not-leak"}
TARGET_DB = {"host": "good.svc", "port": 5432}
REASONS = [{"code": "APP-01-TARGET-DB-UNREACHABLE", "message": "연결 실패", "rule": "APP-01", "effect": "deny"}]
GOOD = {"changes": [{"key": "DB_HOST", "value": "good.svc"}], "cause": "c", "impact": "i", "verification": "v"}


def fake_codex(tmp_path: Path, *, output: object = GOOD, edit: dict | None = None, exit_code: int = 0, sleep: float = 0) -> Path:
    """인자·환경·stdin·입력 파일을 record.json 에 남기고, 지정한 대로 파일을 고치고 답한다."""
    record = tmp_path / "record.json"
    script = tmp_path / "codex"
    body = json.dumps(output) if not isinstance(output, str) else output
    script.write_text(f"""#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
stdin = sys.stdin.read()  # 닫혀 있지 않으면 여기서 멈춘다
target = os.path.join(args[args.index('-C') + 1], 'config', 'target.json')
seen = json.load(open(target))
json.dump({{'args': args, 'env': dict(os.environ), 'stdin': stdin, 'seen': seen,
            'schema': json.load(open(args[args.index('--output-schema') + 1])),
            'tree': sorted(os.listdir(args[args.index('-C') + 1]))}}, open({str(record)!r}, 'w'))
time.sleep({sleep})
edit = {json.dumps(edit if edit is not None else {"DB_HOST": "good.svc"})!r}
seen.update(json.loads(edit))
json.dump(seen, open(target, 'w'))
if {exit_code} == 0:
    open(args[args.index('--output-last-message') + 1], 'w').write({body!r})
print('noise on stderr', file=sys.stderr)
sys.exit({exit_code})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def propose(script: Path, *, timeout: float = 20.0, env: dict | None = None):
    environ = {"PATH": os.environ["PATH"], "VAULT_TOKEN": "vault-secret", "SLACK_BOT_TOKEN": "slack-secret",
               "SAMPLE_APP_DATABASE_URL": "postgresql://u:db-secret@h/d", "CODEX_API_KEY": "codex-key", **(env or {})}
    return CodexProposer(CodexSettings(bin=str(script), timeout=timeout), env=environ).propose(
        current=CURRENT, reasons=REASONS, target_db=TARGET_DB)


def record(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "record.json").read_text())


def test_command_flags_and_output(tmp_path: Path) -> None:
    proposal, file_changes = propose(fake_codex(tmp_path))
    assert proposal.values() == {"DB_HOST": "good.svc"} and proposal.cause == "c"
    assert file_changes == {"DB_HOST": "good.svc"}
    args = record(tmp_path)["args"]
    assert args[0] == "exec"
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert args[args.index("-c") + 1] == "approval_policy='never'"
    for flag in ("--ephemeral", "--ignore-user-config", "--skip-git-repo-check"):
        assert flag in args


def test_stdin_is_closed(tmp_path: Path) -> None:
    """stdin 이 열려 있으면 가짜 codex 의 read() 가 끝나지 않아 시간 초과로 실패한다."""
    propose(fake_codex(tmp_path), timeout=10)
    assert record(tmp_path)["stdin"] == ""


def test_secret_never_reaches_codex(tmp_path: Path) -> None:
    propose(fake_codex(tmp_path))
    rec = record(tmp_path)
    assert rec["seen"] == {"DB_HOST": "wrong.svc", "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u"}
    assert "DB_PASSWORD" not in json.dumps(rec["args"]) and "must-not-leak" not in json.dumps(rec)
    for name in ("VAULT_TOKEN", "SLACK_BOT_TOKEN", "SAMPLE_APP_DATABASE_URL"):
        assert name not in rec["env"]
    assert rec["env"]["CODEX_API_KEY"] == "codex-key"


def test_prompt_has_deny_reason_and_plan_target(tmp_path: Path) -> None:
    propose(fake_codex(tmp_path))
    prompt = record(tmp_path)["args"][-1]
    assert "APP-01-TARGET-DB-UNREACHABLE" in prompt and "good.svc" in prompt and "5432" in prompt


def test_schema_file_is_the_fixed_schema(tmp_path: Path) -> None:
    propose(fake_codex(tmp_path))
    assert record(tmp_path)["schema"] == OUTPUT_SCHEMA


def test_file_edit_beyond_report_is_exposed(tmp_path: Path) -> None:
    _, file_changes = propose(fake_codex(tmp_path, edit={"DB_HOST": "good.svc", "DB_NAME": "other"}))
    assert file_changes == {"DB_HOST": "good.svc", "DB_NAME": "other"}


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        "[]",
        {"changes": []},
        {**GOOD, "extra": "x"},
        {**GOOD, "changes": {"DB_HOST": "good.svc"}},
        {**GOOD, "changes": [{"key": "DB_HOST"}]},
        {**GOOD, "changes": [{"key": "DB_PORT", "value": 5432}]},
        {**GOOD, "changes": [{"key": "DB_HOST", "value": "x", "note": "y"}]},
        {**GOOD, "cause": ""},
        {**GOOD, "impact": None},
    ],
)
def test_schema_violation_raises(tmp_path: Path, output: object) -> None:
    with pytest.raises(ProposalInvalid):
        propose(fake_codex(tmp_path, output=output))


def test_empty_changes_parse_for_static_check_to_reject() -> None:
    assert parse_output(json.dumps({**GOOD, "changes": []})).changes == ()


def test_nonzero_exit_raises(tmp_path: Path) -> None:
    with pytest.raises(ProposerError):
        propose(fake_codex(tmp_path, exit_code=3))


def test_timeout_raises(tmp_path: Path) -> None:
    with pytest.raises(ProposerError, match="시간 초과"):
        propose(fake_codex(tmp_path, sleep=5), timeout=1)


def test_missing_binary_raises(tmp_path: Path) -> None:
    with pytest.raises(ProposerError):
        propose(tmp_path / "no-such-codex")


def test_symlink_to_file_outside_job_is_refused(tmp_path: Path) -> None:
    """AI 쪽이 target.json 을 토큰 파일 링크로 바꿔도 읽지 않는다."""
    secret = tmp_path / "token"
    secret.write_text('{"DB_HOST": "stolen"}')
    script = tmp_path / "codex"
    script.write_text(f"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
target = os.path.join(args[args.index('-C') + 1], 'config', 'target.json')
os.remove(target)
os.symlink({str(secret)!r}, target)
open(args[args.index('--output-last-message') + 1], 'w').write({json.dumps(GOOD)!r})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(ProposalInvalid, match="밖을 가리킨다"):
        propose(script)


# ---------------------------------------------------------------------------
# 사이드카: 에이전트는 공유 폴더에 작업을 놓고, 비밀값 없는 옆 컨테이너가 실행한다
# ---------------------------------------------------------------------------


def sidecar_proposer(shared: Path, timeout: float = 20.0) -> CodexProposer:
    settings = CodexSettings(runner="sidecar", shared_dir=str(shared), timeout=timeout)
    return CodexProposer(settings, runner=SidecarCodexRunner(settings, poll_interval=0.02))


def run_sidecar_once(shared: Path, script: Path, env: dict, stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop.is_set():
            process_pending(shared, bin_path=str(script), timeout=10, env=env)
            time.sleep(0.02)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    path = tmp_path / "shared"
    path.mkdir()
    return path


def test_sidecar_round_trip_uses_container_as_boundary(tmp_path: Path, shared: Path) -> None:
    stop = threading.Event()
    env = {"PATH": os.environ["PATH"], "CODEX_API_KEY": "codex-key", "VAULT_TOKEN": "must-not-pass"}
    thread = run_sidecar_once(shared, fake_codex(tmp_path), env, stop)
    try:
        proposal, file_changes = sidecar_proposer(shared).propose(current=CURRENT, reasons=REASONS, target_db=TARGET_DB)
    finally:
        stop.set()
        thread.join()
    assert proposal.values() == {"DB_HOST": "good.svc"} and file_changes == {"DB_HOST": "good.svc"}
    rec = record(tmp_path)
    assert rec["args"][rec["args"].index("--sandbox") + 1] == "danger-full-access"
    assert rec["stdin"] == "" and "VAULT_TOKEN" not in rec["env"] and rec["env"]["CODEX_API_KEY"] == "codex-key"
    assert "must-not-leak" not in json.dumps(rec)
    assert list(shared.iterdir()) == []  # 작업 폴더를 남기지 않는다


def test_sidecar_nonzero_exit_raises(tmp_path: Path, shared: Path) -> None:
    stop = threading.Event()
    thread = run_sidecar_once(shared, fake_codex(tmp_path, exit_code=4), {"PATH": os.environ["PATH"]}, stop)
    try:
        with pytest.raises(ProposerError, match="종료 코드 4"):
            sidecar_proposer(shared).propose(current=CURRENT, reasons=REASONS, target_db=TARGET_DB)
    finally:
        stop.set()
        thread.join()


def test_sidecar_down_times_out(shared: Path) -> None:
    with pytest.raises(ProposerError, match="시간 초과"):
        sidecar_proposer(shared, timeout=0.1).propose(current=CURRENT, reasons=REASONS, target_db=TARGET_DB)


def test_runner_ignores_arguments_in_request(tmp_path: Path, shared: Path) -> None:
    """요청 파일은 prompt·model 만 전한다. 임의 인자를 넣어도 명령에 들어가지 않는다."""
    script = fake_codex(tmp_path)
    job = shared / "job-x"
    (job / "workspace" / "config").mkdir(parents=True)
    (job / "workspace" / "config" / "target.json").write_text("{}")
    (job / "schema.json").write_text("{}")
    (job / "request.json").write_text(json.dumps({"prompt": "p", "model": None, "args": ["--evil"], "bin": "/bin/sh"}))
    process_pending(shared, bin_path=str(script), timeout=10, env={"PATH": os.environ["PATH"]})
    assert "--evil" not in record(tmp_path)["args"]
    assert json.loads((job / "done.json").read_text())["exit_code"] == 0
    process_pending(shared, bin_path="/no/such/bin", timeout=10, env={})  # done 이 있으면 다시 실행하지 않는다


def test_build_proposer_requires_shared_dir() -> None:
    with pytest.raises(ProposerError):
        build_proposer(CodexSettings(runner="sidecar"))
    with pytest.raises(ProposerError):
        build_proposer(CodexSettings(runner="nope"))
