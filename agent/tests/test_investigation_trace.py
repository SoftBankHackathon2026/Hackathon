from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import runs as R
from agent.config import CodexSettings
from agent.fakes import FixHostProposer
from agent.hypotheses import INCONCLUSIVE, INVALIDATED, VALIDATED, build_hypotheses, cause_of
from agent.probe import ProbeResult, classify
from agent.proposer import CodexProposer, OpenSandboxCodexRunner
from agent.trace import MAX_STEPS, parse_trace

from test_proposer_opensandbox import GOOD, KEY, FakeSandbox, settings

EVENTS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "item.started", "item": {"id": "1", "type": "command_execution", "command": "cat x"}},
    {"type": "item.completed", "item": {"id": "0", "type": "reasoning", "text": f"**설정 파일 확인**\n먼저 읽어 본다 {KEY}"}},
    {"type": "item.completed", "item": {"id": "1", "type": "command_execution",
                                        "command": "/bin/sh -lc \"sed -n '1,200p' ./config/target.json\"", "aggregated_output": "SECRET-OUT",
                                        "exit_code": 0}},
    {"type": "item.completed", "item": {"id": "2", "type": "command_execution", "command": ["/bin/bash", "-lc", "false"], "exit_code": 1}},
    {"type": "item.completed", "item": {"id": "3", "type": "file_change",
                                        "changes": [{"path": "/work/job/workspace/config/target.json", "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "5", "type": "agent_message",
                                        "text": json.dumps({"changes": [], "cause": "파일을 읽고 비교할게요.", "impact": "필요한 값만 바꿀게요."})}},
    {"type": "item.completed", "item": {"id": "6", "type": "agent_message", "text": "중간 메모"}},
    {"type": "item.completed", "item": {"id": "4", "type": "agent_message", "text": json.dumps(GOOD)}},
    {"type": "turn.completed", "usage": {"input_tokens": 1}},
]
STEPS = [
    {"kind": "think", "text": "설정 파일 확인 먼저 읽어 본다 ***"},
    {"kind": "run", "text": "sed -n '1,200p' ./config/target.json", "ok": True},
    {"kind": "run", "text": "false", "ok": False},
    {"kind": "edit", "text": "config/target.json"},
    {"kind": "say", "text": "파일을 읽고 비교할게요. 필요한 값만 바꿀게요."},
    {"kind": "say", "text": "중간 메모"},
]


def hide(text: str) -> str:
    return text.replace(KEY, "***")


def jsonl(events: list[dict]) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"


def test_trace_keeps_thinking_commands_and_edits_only() -> None:
    steps = parse_trace(jsonl(EVENTS), hide)
    assert steps == STEPS
    assert "SECRET-OUT" not in json.dumps(steps)


@pytest.mark.parametrize("glue", ["", "\n"])
def test_trace_survives_chunked_or_glued_stream(glue: str) -> None:
    text = jsonl(EVENTS)
    chunks = [text[i:i + 37] for i in range(0, len(text), 37)]
    assert parse_trace('{"broken' + "".join(chunks).replace("\n", glue) + "{broken", hide) == STEPS


def test_trace_reads_older_item_shapes_scrubs_and_clips() -> None:
    old = {"type": "item.completed", "item": {"id": "0", "details": {"item_type": "reasoning", "text": f"key {KEY} " + "x" * 900}}}
    steps = parse_trace(jsonl([old]), lambda s: s.replace(KEY, "***"))
    glob = parse_trace(jsonl([{"type": "item.completed", "item": {"type": "command_execution", "command": "ls **/*.json", "exit_code": 0}}]))
    assert glob[0]["text"] == "ls **/*.json"
    assert steps[0]["kind"] == "think" and KEY not in steps[0]["text"] and steps[0]["text"].startswith("key ***")
    assert len(steps[0]["text"]) == 400 and steps[0]["text"].endswith("…")
    many = [{"type": "item.completed", "item": {"type": "reasoning", "text": f"s{i}"}} for i in range(MAX_STEPS + 5)]
    folded = parse_trace(jsonl(many))
    assert len(folded) == MAX_STEPS and folded[-1]["text"] == "이후 6단계는 생략했어요."


def fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "codex"
    script.write_text(f"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
assert "--json" in args
target = os.path.join(args[args.index('-C') + 1], 'config', 'target.json')
seen = json.load(open(target)); seen["DB_HOST"] = "good.svc"; json.dump(seen, open(target, 'w'))
open(args[args.index('--output-last-message') + 1], 'w').write({json.dumps(json.dumps(GOOD))})
sys.stdout.write({json.dumps(jsonl(EVENTS))})
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


CURRENT = {"DB_HOST": "wrong.svc", "DB_PORT": "5432", "DB_PASSWORD": "must-not-leak"}


def test_local_runner_records_trace_and_file_diff(tmp_path: Path) -> None:
    proposer = CodexProposer(CodexSettings(bin=str(fake_codex(tmp_path)), timeout=20, model="m"),
                             env={"PATH": os.environ["PATH"], "CODEX_API_KEY": KEY})
    proposer.propose(current=CURRENT, reasons=[], target_db={"host": "good.svc", "port": 5432})
    trace = proposer.last_trace
    assert trace["steps"] == STEPS and trace["runner"] == "local" and trace["model"] == "m" and trace["image"] is None
    assert json.loads(trace["file"]["before"]) == {"DB_HOST": "wrong.svc", "DB_PORT": "5432"}
    assert json.loads(trace["file"]["after"])["DB_HOST"] == "good.svc"
    assert "must-not-leak" not in json.dumps(trace) and trace["seconds"] >= 0
    assert KEY not in json.dumps(trace)


class StreamingSandbox(FakeSandbox):
    def run(self, cmd, opts=None):
        result = super().run(cmd, opts)
        if cmd[0] == "codex":
            text = jsonl(EVENTS)
            result.logs.stdout = [SimpleNamespace(text=text[i:i + 50]) for i in range(0, len(text), 50)]
        return result


def test_opensandbox_runner_keeps_trace_even_when_codex_fails() -> None:
    sandbox = StreamingSandbox()
    runner = OpenSandboxCodexRunner(settings(), env={"CODEX_API_KEY": KEY}, create=lambda s, e: sandbox)
    proposer = CodexProposer(settings(), runner=runner, env={"CODEX_API_KEY": KEY})
    proposer.propose(current=CURRENT, reasons=[], target_db={"host": "good.svc", "port": 5432})
    assert proposer.last_trace["steps"] == STEPS
    assert proposer.last_trace["image"] == settings().sandbox_image

    failing = StreamingSandbox(exit_code=2, output=None)
    runner = OpenSandboxCodexRunner(settings(), env={"CODEX_API_KEY": KEY}, create=lambda s, e: failing)
    proposer = CodexProposer(settings(), runner=runner, env={"CODEX_API_KEY": KEY})
    with pytest.raises(Exception):
        proposer.propose(current=CURRENT, reasons=[], target_db={"host": "good.svc", "port": 5432})
    assert proposer.last_trace["steps"] == STEPS


PLAN = {"DB_HOST": "good.svc", "DB_PORT": "5432", "DB_NAME": "app", "DB_USER": "app_user"}
RIGHT = dict(PLAN)


def statuses(result: ProbeResult, current: dict, planned: dict = PLAN) -> dict[str, str]:
    return {h["id"]: h["status"] for h in build_hypotheses(result, current, planned)}


def fail(step: str, exc: BaseException) -> ProbeResult:
    return ProbeResult(False, step, type(exc).__name__, classify(step, exc))


def test_dns_failure_points_at_the_wrong_host() -> None:
    got = statuses(fail("tcp", socket.gaierror("x")), {**RIGHT, "DB_HOST": "old.svc"})
    assert got == {"host": VALIDATED, "port": INCONCLUSIVE, "name": INCONCLUSIVE, "user": INCONCLUSIVE,
                   "password": INCONCLUSIVE, "server": INCONCLUSIVE}


def test_refused_port_and_unreachable_server() -> None:
    assert statuses(fail("tcp", ConnectionRefusedError()), {**RIGHT, "DB_PORT": "5433"})["port"] == VALIDATED
    same = statuses(fail("tcp", TimeoutError()), RIGHT)
    assert (same["host"], same["port"], same["server"]) == (INVALIDATED, INVALIDATED, VALIDATED)


def test_missing_database_validates_name_and_clears_network_and_auth() -> None:
    result = fail("login", SimpleNamespace(sqlstate="3D000"))
    got = statuses(result, {**RIGHT, "DB_NAME": "app_prod"})
    assert got == {"host": INVALIDATED, "port": INVALIDATED, "name": VALIDATED, "user": INVALIDATED,
                   "password": INVALIDATED, "server": INVALIDATED}
    hyps = build_hypotheses(result, {**RIGHT, "DB_NAME": "app_prod"}, PLAN)
    name = next(h for h in hyps if h["id"] == "name")
    assert name["key"] == "DB_NAME" and "app_prod" in name["evidence"] and "SQLSTATE 3D000" in name["evidence"]
    assert statuses(result, RIGHT)["name"] == INCONCLUSIVE


def test_auth_failures_split_user_and_password() -> None:
    role = statuses(fail("login", Exception('FATAL:  role "x" does not exist')), {**RIGHT, "DB_USER": "x"})
    assert (role["user"], role["name"], role["password"]) == (VALIDATED, INCONCLUSIVE, INCONCLUSIVE)
    password = build_hypotheses(fail("login", SimpleNamespace(sqlstate="28P01")), RIGHT, PLAN)
    secret = next(h for h in password if h["id"] == "password")
    assert secret["status"] == VALIDATED and secret["key"] is None and "AI가 다루지 않아요" in secret["evidence"]


def test_tls_and_healthy_connection() -> None:
    tls = statuses(fail("login", Exception("SSL error: certificate verify failed")), RIGHT, {**PLAN, "DB_SSLMODE": "verify-full"})
    assert tls["sslmode"] == VALIDATED
    assert "sslmode" not in statuses(ProbeResult(True, "done"), RIGHT)
    healthy = statuses(ProbeResult(True, "done"), RIGHT, {**PLAN, "DB_SSLMODE": "require"})
    assert set(healthy.values()) == {INVALIDATED}
    assert cause_of(ProbeResult(False, "query", "X", None)) == "login_other"
    assert statuses(fail("login", SimpleNamespace(sqlstate="57P03")), RIGHT)["server"] == VALIDATED


class TracingProposer(FixHostProposer):
    def __init__(self, *, bad: bool = False) -> None:
        super().__init__()
        self.bad = bad
        self.last_trace = {"runner": "opensandbox", "steps": STEPS, "file": {"path": "config/target.json", "before": "b", "after": "a"}}

    def propose(self, **kwargs):
        proposal, changes = super().propose(**kwargs)
        return (proposal, {"DB_PASSWORD": "x"}) if self.bad else (proposal, changes)


def test_run_records_hypotheses_and_sandbox_trace(make_world) -> None:
    w = make_world(auto_apply=False, proposer=TracingProposer())
    run = w.remediator.run()
    assert run.state == R.WAITING_APPROVAL
    hyps = {h["id"]: h for h in run.detail["hypotheses"]}
    assert hyps["host"]["status"] == VALIDATED and hyps["host"]["key"] == "DB_HOST"
    assert run.detail["sandbox"]["steps"] == STEPS and run.detail["sandbox"]["file"]["after"] == "a"


def test_rejected_run_keeps_steps_but_drops_file(make_world) -> None:
    w = make_world(auto_apply=False, proposer=TracingProposer(bad=True))
    run = w.remediator.run()
    assert run.state == R.REJECTED
    assert run.detail["sandbox"]["steps"] == STEPS and "file" not in run.detail["sandbox"]
