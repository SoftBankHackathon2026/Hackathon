"""공통 픽스처: 설정, 파일 저장소, 가짜 탐침·제안기, 로컬 OPA 평가기."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping

import pytest

from agent.config import AgentConfig, parse_config
from agent.notify import LogNotifier
from agent.probe import ProbeResult
from agent.proposer import ProposalContext, ProposerError
from agent.scope import Change, Proposal
from agent.stores import FileStore
from migration import policy_client as pc

POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"
GOOD_HOST = "db.good.internal"

requires_opa = pytest.mark.skipif(shutil.which("opa") is None, reason="opa CLI 가 없어 정책 평가 테스트 생략")


def raw_config(tmp_path: Path, **override: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "job_id": "forward_001",
        "target_env": "test",
        "store": {"kind": "file", "file": {"path": str(tmp_path / "target.json")}},
        "scope": {"allowed_keys": ["DB_HOST", "DB_PORT"], "secret_keys": ["DB_PASSWORD"], "table": "orders"},
        "plan": {
            "source": "onprem", "target": "aws", "approved_locations": ["ap-northeast-2"],
            "limits": {"evidence_ttl_seconds": 300, "write_wait_max_seconds": 30},
            "target_db": {"host": GOOD_HOST, "port": 5432},
            "remediation": {"auto_apply": True},
        },
        "evidence_extra": str(tmp_path / "evidence_extra.json"),
        "opa": {"policy_dir": str(POLICY_DIR)},
        "codex": {"bin": "codex", "max_attempts": 2},
        "state_dir": str(tmp_path / "state"),
    }
    raw.update(override)
    return raw


def write_evidence_extra(path: Path) -> None:
    checks = {
        "target_db_exposure": pc.build_check("pass", {"public": False}, source="evidence.test"),
        "target_location": pc.build_check("pass", {"location": "ap-northeast-2"}, source="evidence.test"),
    }
    path.write_text(json.dumps(checks), encoding="utf-8")


@pytest.fixture
def target_values() -> dict[str, str]:
    return {"DB_HOST": "wrong.internal", "DB_PORT": "5432", "DB_NAME": "migration_demo",
            "DB_USER": "migration_verify", "DB_PASSWORD": "s3cret-value"}


@pytest.fixture
def cfg(tmp_path: Path, target_values: dict[str, str]) -> AgentConfig:
    (tmp_path / "target.json").write_text(json.dumps(target_values), encoding="utf-8")
    write_evidence_extra(tmp_path / "evidence_extra.json")
    return parse_config(raw_config(tmp_path), base_dir=tmp_path)


@pytest.fixture
def file_store(cfg: AgentConfig) -> FileStore:
    return FileStore(cfg.store.path or "")


class FakeProber:
    """GOOD_HOST 로만 연결되는 탐침. 호출 이력을 남긴다."""

    def __init__(self, good_hosts: tuple[str, ...] = (GOOD_HOST,), *, order_ok: bool = True) -> None:
        self.good_hosts = good_hosts
        self.order_ok = order_ok
        self.calls: list[dict[str, str]] = []

    def probe(self, values: Mapping[str, str]) -> ProbeResult:
        self.calls.append(dict(values))
        host = values.get("DB_HOST", "")
        port = int(values.get("DB_PORT", "5432") or 0)
        if host in self.good_hosts:
            return ProbeResult(host, port, True, True, self.order_ok, None if self.order_ok else "query: relation missing")
        return ProbeResult(host, port, False, False, False, f'tcp: could not resolve "{host}"')


class StaticProposer:
    """정해진 제안을 돌려주는 가짜 Codex. errors 로 실패 순서를 흉내 낸다."""

    name = "static"

    def __init__(self, changes: list[Change] | None = None, *, errors: list[str] | None = None,
                 cannot_fix: str | None = None) -> None:
        self.changes = changes if changes is not None else [Change("DB_HOST", "wrong.internal", GOOD_HOST)]
        self.errors = list(errors or [])
        self.cannot_fix = cannot_fix
        self.contexts: list[ProposalContext] = []

    def propose(self, ctx: ProposalContext) -> Proposal:
        self.contexts.append(ctx)
        if self.errors:
            raise ProposerError(self.errors.pop(0))
        return Proposal(tuple(self.changes), "분석", "호스트가 틀림", "DB_HOST 교체", "재탐침", self.cannot_fix, proposer=self.name)


@pytest.fixture
def notifier() -> LogNotifier:
    return LogNotifier()
