"""CodexProposer: 실제 codex 대신 같은 인터페이스(exec … -o 파일, config/target.json 편집)를 흉내 내는 스크립트로 검사한다."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agent.config import CodexConfig
from agent.proposer import CodexProposer, ProposalContext, ProposerError, render_context, values_for_ai
from tests.conftest import GOOD_HOST

FAKE = r'''#!/usr/bin/env python3
"""가짜 codex: exec 인자를 받아 config/target.json 을 고치고 -o 파일에 JSON 을 쓴다. 동작은 FAKE_CODEX_MODE 로 바꾼다."""
import json, os, sys
args = sys.argv[1:]
assert args[0] == "exec", args
cwd = args[args.index("-C") + 1]
out = args[args.index("-o") + 1]
assert "--output-schema" in args and "--sandbox" in args and args[args.index("--sandbox") + 1] == "workspace-write"
assert "approval_policy='never'" in args and "--ephemeral" in args and "--ignore-user-config" in args
mode = os.environ.get("FAKE_CODEX_MODE", "fix")
# 비밀값·다른 토큰이 넘어오면 안 된다
for k in os.environ:
    assert not k.startswith(("VAULT_", "SLACK_", "AWS_")), k
target = os.path.join(cwd, "config", "target.json")
data = json.load(open(target))
assert "DB_PASSWORD" not in data
ctx = open(os.path.join(cwd, "CONTEXT.md")).read()
assert "s3cret" not in ctx
doc = {"analysis": "탐침 오류가 host 를 가리킴", "root_cause": "DB_HOST 오타", "impact": "DB_HOST 교체",
       "verification_plan": "재탐침", "cannot_fix_reason": None}
if mode == "fix":
    data["DB_HOST"] = os.environ["FAKE_GOOD_HOST"]
elif mode == "out_of_scope":
    data["DB_NAME"] = "other"
elif mode == "cannot":
    doc["cannot_fix_reason"] = "허용 값 목록에 맞는 호스트가 없다"
elif mode == "stray":
    open(os.path.join(cwd, "notes.txt"), "w").write("x")
elif mode == "bad_json_out":
    open(out, "w").write("not json"); sys.exit(0)
elif mode == "crash":
    sys.stderr.write("boom"); sys.exit(3)
elif mode == "no_output":
    sys.exit(0)
json.dump(data, open(target, "w"))
json.dump(doc, open(out, "w"))
'''


@pytest.fixture
def fake_codex(tmp_path: Path, monkeypatch):
    script = tmp_path / "codex"
    script.write_text(FAKE, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_GOOD_HOST", GOOD_HOST)
    monkeypatch.setenv("VAULT_TOKEN", "must-not-leak")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "must-not-leak")
    return str(script)


def _ctx(cfg, target_values):
    return ProposalContext(
        job_id="forward_001", target_env="test", values_for_ai=values_for_ai(target_values, cfg.scope),
        reason_codes=["APP-01-TARGET-DB-UNREACHABLE"], violations=[{"code": "APP-01-TARGET-DB-UNREACHABLE", "message": "m"}],
        probe_detail={"error": 'tcp: could not resolve "wrong.internal"', "host": "wrong.internal"},
        scope=cfg.scope, plan=cfg.plan,
    )


def test_values_for_ai_drops_secrets(cfg, target_values):
    v = values_for_ai(target_values, cfg.scope)
    assert "DB_PASSWORD" not in v and v["DB_HOST"] == "wrong.internal"


def test_render_context_mentions_scope_and_target(cfg, target_values):
    text = render_context(_ctx(cfg, target_values))
    assert GOOD_HOST in text and "DB_HOST, DB_PORT" in text and "s3cret" not in text


def test_codex_fix(cfg, target_values, fake_codex, tmp_path):
    p = CodexProposer(CodexConfig(bin=fake_codex, model="test-model"), workdir_root=tmp_path).propose(_ctx(cfg, target_values))
    assert [c.as_dict() for c in p.changes] == [{"key": "DB_HOST", "old": "wrong.internal", "new": GOOD_HOST}]
    assert p.model == "test-model" and p.proposer == "codex" and p.cannot_fix_reason is None


@pytest.mark.parametrize("mode,expect", [
    ("crash", "종료 코드 3"), ("no_output", "최종 응답 파일"), ("bad_json_out", "JSON 이 아니다"), ("stray", "허용되지 않은 파일"),
])
def test_codex_failures(cfg, target_values, fake_codex, tmp_path, monkeypatch, mode, expect):
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    with pytest.raises(ProposerError, match=expect):
        CodexProposer(CodexConfig(bin=fake_codex), workdir_root=tmp_path).propose(_ctx(cfg, target_values))


def test_codex_out_of_scope_change_is_reported_not_hidden(cfg, target_values, fake_codex, tmp_path, monkeypatch):
    """범위 밖 변경은 제안기가 숨기지 않고 그대로 돌려준다. 거르는 것은 static_checks·정책의 일이다."""
    monkeypatch.setenv("FAKE_CODEX_MODE", "out_of_scope")
    p = CodexProposer(CodexConfig(bin=fake_codex), workdir_root=tmp_path).propose(_ctx(cfg, target_values))
    assert [c.key for c in p.changes] == ["DB_NAME"]


def test_codex_cannot_fix(cfg, target_values, fake_codex, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_MODE", "cannot")
    p = CodexProposer(CodexConfig(bin=fake_codex), workdir_root=tmp_path).propose(_ctx(cfg, target_values))
    assert p.changes == () and p.cannot_fix_reason


def test_codex_missing_binary(cfg, target_values, tmp_path):
    with pytest.raises(ProposerError, match="실행 파일이 없다"):
        CodexProposer(CodexConfig(bin=str(tmp_path / "nope")), workdir_root=tmp_path).propose(_ctx(cfg, target_values))


def test_codex_timeout(cfg, target_values, tmp_path):
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    with pytest.raises(ProposerError, match="끝나지 않았다"):
        CodexProposer(CodexConfig(bin=str(slow), timeout_seconds=0.5), workdir_root=tmp_path).propose(_ctx(cfg, target_values))
