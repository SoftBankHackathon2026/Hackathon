from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from agent.config import CodexSettings
from agent.proposer import (
    MAX_RESULT_BYTES,
    CodexProposer,
    OpenSandboxCodexRunner,
    ProposalInvalid,
    ProposerError,
    build_proposer,
)

CURRENT = {"DB_HOST": "wrong.svc", "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u", "DB_PASSWORD": "must-not-leak"}
TARGET_DB = {"host": "good.svc", "port": 5432}
REASONS = [{"code": "APP-01-TARGET-DB-UNREACHABLE", "message": "연결 실패", "rule": "APP-01", "effect": "deny"}]
GOOD = {"changes": [{"key": "DB_HOST", "value": "good.svc"}], "cause": "c", "impact": "i", "verification": "v"}
KEY = "sk-test-key-1234567890"
OUTPUT = "/work/job/last-message.json"
TARGET = "/work/job/workspace/config/target.json"


def execution(code: int = 0, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(exit_code=code, logs=SimpleNamespace(stdout=[], stderr=[SimpleNamespace(text=stderr)]))


@dataclass
class FakeSandbox:
    output: str | None = json.dumps(GOOD)
    edit: dict = field(default_factory=lambda: {"DB_HOST": "good.svc"})
    exit_code: int = 0
    stderr: str = ""
    files_store: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    killed: bool = False
    closed: bool = False

    @property
    def files(self) -> "FakeSandbox":
        return self

    @property
    def commands(self) -> "FakeSandbox":
        return self

    def write_file(self, path: str, data: str) -> None:
        self.files_store[path] = data.encode()

    def read_bytes(self, path: str, limit: int | None = None) -> bytes:
        if path not in self.files_store:
            raise FileNotFoundError(path)
        return self.files_store[path]

    def run(self, cmd: list[str], opts=None) -> SimpleNamespace:
        self.calls.append((cmd, opts))
        if cmd[0] != "codex":
            return execution()
        seen = json.loads(self.files_store[TARGET])
        seen.update(self.edit)
        self.files_store[TARGET] = json.dumps(seen).encode()
        if self.output is not None:
            self.files_store[OUTPUT] = self.output.encode()
        return execution(self.exit_code, self.stderr)

    def kill(self) -> None:
        self.killed = True

    def close(self) -> None:
        self.closed = True


def settings(**kw) -> CodexSettings:
    return CodexSettings(**{"runner": "opensandbox", "sandbox_url": "http://127.0.0.1:8090", "model": "m", **kw})


def proposer(sandbox: FakeSandbox, env: dict | None = None, created: list | None = None) -> CodexProposer:
    def create(s, e):
        if created is not None:
            created.append((s, e))
        return sandbox

    runner = OpenSandboxCodexRunner(settings(), env={"CODEX_API_KEY": KEY} if env is None else env, create=create)
    return CodexProposer(settings(), runner=runner)


def propose(p: CodexProposer):
    return p.propose(current=CURRENT, reasons=REASONS, target_db=TARGET_DB)


def codex_call(sandbox: FakeSandbox):
    return next((cmd, opts) for cmd, opts in sandbox.calls if cmd[0] == "codex")


def test_round_trip_runs_codex_inside_sandbox() -> None:
    sandbox = FakeSandbox()
    env = {"CODEX_API_KEY": KEY, "VAULT_TOKEN": "hvs.secret", "PATH": "/host/bin", "OPENSANDBOX_API_KEY": "os-key"}
    proposal, file_changes = propose(proposer(sandbox, env=env))

    assert proposal.values() == {"DB_HOST": "good.svc"}
    assert file_changes == {"DB_HOST": "good.svc"}
    cmd, opts = codex_call(sandbox)
    assert cmd[cmd.index("--sandbox") + 1] == "danger-full-access"
    assert cmd[cmd.index("--model") + 1] == "m"
    assert cmd[cmd.index("-C") + 1] == "/work/job/workspace"
    assert opts.envs == {"CODEX_API_KEY": KEY, "HOME": "/tmp/codex-home"}
    assert (opts.uid, opts.gid) == (10001, 10001)
    assert sandbox.killed and sandbox.closed


def test_secret_values_never_reach_sandbox() -> None:
    sandbox = FakeSandbox()
    propose(proposer(sandbox))

    uploaded = b"".join(v for k, v in sandbox.files_store.items() if k != OUTPUT)
    assert b"must-not-leak" not in uploaded
    assert all(b"must-not-leak" not in json.dumps(cmd).encode() for cmd, _ in sandbox.calls)


def test_nonzero_exit_raises_and_scrubs_key(caplog: pytest.LogCaptureFixture) -> None:
    sandbox = FakeSandbox(exit_code=3, output=None, stderr=f"401 Incorrect API key provided: {KEY}")
    with caplog.at_level(logging.ERROR), pytest.raises(ProposerError, match="종료 코드 3"):
        propose(proposer(sandbox))
    assert KEY not in caplog.text
    assert sandbox.killed


def test_missing_codex_key_does_not_create_sandbox() -> None:
    created: list = []
    with pytest.raises(ProposerError, match="CODEX_API_KEY"):
        propose(proposer(FakeSandbox(), env={}, created=created))
    assert created == []


def test_create_failure_hides_detail() -> None:
    def create(s, e):
        raise RuntimeError("http://user:secret@host refused")

    runner = OpenSandboxCodexRunner(settings(), env={"CODEX_API_KEY": KEY}, create=create)
    with pytest.raises(ProposerError) as info:
        propose(CodexProposer(settings(), runner=runner))
    assert "secret" not in str(info.value)


def test_missing_final_message_is_invalid() -> None:
    sandbox = FakeSandbox(output=None)
    with pytest.raises(ProposalInvalid, match="최종 메시지"):
        propose(proposer(sandbox))
    assert sandbox.killed


def test_oversized_output_is_rejected() -> None:
    sandbox = FakeSandbox(output="x" * (MAX_RESULT_BYTES + 10))
    with pytest.raises(ProposalInvalid, match="너무 크다"):
        propose(proposer(sandbox))


def test_file_edit_outside_report_is_visible_to_static_check() -> None:
    sandbox = FakeSandbox(edit={"DB_HOST": "good.svc", "DB_USER": "root"})
    _, file_changes = propose(proposer(sandbox))
    assert file_changes == {"DB_HOST": "good.svc", "DB_USER": "root"}


def test_build_requires_model_and_url() -> None:
    with pytest.raises(ProposerError, match="model"):
        build_proposer(settings(model=None))
    with pytest.raises(ProposerError, match="sandbox_url"):
        build_proposer(settings(sandbox_url=None))
    assert isinstance(build_proposer(settings()).runner, OpenSandboxCodexRunner)
