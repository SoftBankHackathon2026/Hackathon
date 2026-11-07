"""agent.yaml + 환경변수. 비밀값은 환경변수로만 받고 설정 파일에 두지 않는다."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

DEFAULT_CONFIG_PATH = "/etc/agent/agent.yaml"


class ConfigError(ValueError):
    """설정 오류. 메시지에 비밀값을 넣지 않는다."""


@dataclass(frozen=True)
class SmokeSettings:
    target_url: str
    source_url: str | None = None
    target_only: bool = False  # 대상 앱 DB 조회만 본다. 복제·쓰기는 검증하지 않는다
    stage: str = "pre_cutover"
    token_env: str = "SMOKE_API_TOKEN"
    http_timeout: float = 5.0
    replication_timeout: float = 30.0
    poll_interval: float = 1.0
    error_retries: int = 0
    retry_delay: float = 8.0


@dataclass(frozen=True)
class OpenBaoSettings:
    addr: str
    mount: str
    path: str
    token: str = field(repr=False)
    timeout: float = 5.0


@dataclass(frozen=True)
class CodexSettings:
    bin: str = "codex"
    model: str | None = None
    timeout: float = 300.0
    home: str | None = None  # 로컬 전용. 설정하면 그 CODEX_HOME 의 로그인을 쓴다
    # local: 이 프로세스에서 workspace-write 로 실행 (Mac·개발).
    # sidecar: 비밀값 없는 옆 컨테이너에 맡긴다 (파드). agent.codex_runner 참고.
    runner: str = "local"
    shared_dir: str | None = None
    sandbox_url: str | None = None
    sandbox_api_key_env: str = "OPENSANDBOX_API_KEY"
    sandbox_image: str = "oneport/codex-sandbox:latest"
    sandbox_cpu: str = "1"
    sandbox_memory: str = "1Gi"
    sandbox_uid: int = 10001


@dataclass(frozen=True)
class ProbeSettings:
    sslmode: str = "verify-full"
    sslrootcert: str | None = None
    password_url_env: str = "SAMPLE_APP_DATABASE_URL"
    connect_timeout: float = 5.0
    address_map: Mapping[str, str] = field(default_factory=dict)
    attempts: int = 1
    retry_delay: float = 5.0


@dataclass(frozen=True)
class RestartSettings:
    namespace: str = "hackathon"
    rollout: str = "sample-app"
    selector: str = "app=sample-app"
    timeout: float = 180.0
    poll_interval: float = 3.0
    stuck_after: float = 45.0
    api_server: str | None = None
    token_file: str | None = None
    ca_file: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    job_id: str
    state_dir: Path
    auto_apply: bool
    opa_url: str
    opa_timeout: float
    plan: Mapping[str, Any]
    declared_evidence: Mapping[str, Mapping[str, Any]]
    smoke: SmokeSettings
    openbao: OpenBaoSettings
    codex: CodexSettings
    probe: ProbeSettings
    restart: RestartSettings
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    approve_command: str = "python -m agent approve"
    platform_url: str | None = None
    platform_agent_id: str = "oneport-agent"
    platform_token_env: str = "ONEPORT_INGEST_TOKEN"

    @property
    def target_db(self) -> Mapping[str, Any]:
        return self.plan["target_db"]


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name)
    if not isinstance(value, Mapping):
        raise ConfigError(f"설정에 {name} 절이 없다")
    return value


def _required(section: Mapping[str, Any], key: str, where: str) -> Any:
    value = section.get(key)
    if value in (None, ""):
        raise ConfigError(f"{where}.{key} 가 없다")
    return value


def _smoke_settings(smoke: Mapping[str, Any]) -> SmokeSettings:
    target_only = bool(smoke.get("target_only", False))
    source_url = smoke.get("source_url") or None
    if not target_only and not source_url:
        raise ConfigError("smoke.source_url 이 없다 (smoke.target_only 가 아닐 때 필수)")
    optional = ("stage", "token_env", "http_timeout", "replication_timeout", "poll_interval", "error_retries", "retry_delay")
    return SmokeSettings(
        target_url=str(_required(smoke, "target_url", "smoke")), source_url=source_url, target_only=target_only,
        **{k: smoke[k] for k in optional if k in smoke},
    )


def _thread_ts(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if not re.fullmatch(r"\d{6,}\.\d{1,9}", text):
        raise ConfigError("slack.thread_ts 는 1794036316.116759 같은 메시지 시각이어야 한다")
    return text


def parse_config(raw: Mapping[str, Any], env: Mapping[str, str] | None = None) -> AgentConfig:
    environ = os.environ if env is None else env
    plan = _section(raw, "plan")
    target_db = _section(plan, "target_db")
    _required(target_db, "host", "plan.target_db")
    _required(target_db, "port", "plan.target_db")
    declared = _section(raw, "declared_evidence")
    for name in ("target_db_exposure", "target_location"):
        _section(declared, name)

    bao = _section(raw, "openbao")
    token = environ.get(str(bao.get("token_env", "VAULT_TOKEN")), "")
    if not token:
        raise ConfigError(f"환경변수 {bao.get('token_env', 'VAULT_TOKEN')} 에 OpenBao 토큰이 없다")
    # Secret ai-agent-store 의 값이 환경변수로 들어오면 그것이 우선한다.
    openbao = OpenBaoSettings(
        addr=environ.get("VAULT_ADDR") or _required(bao, "addr", "openbao"),
        mount=environ.get("VAULT_MOUNT") or _required(bao, "mount", "openbao"),
        path=environ.get("VAULT_PATH") or _required(bao, "path", "openbao"),
        token=token,
        timeout=float(bao.get("timeout", 5.0)),
    )

    smoke = _section(raw, "smoke")
    return AgentConfig(
        job_id=str(_required(raw, "job_id", "root")),
        state_dir=Path(str(raw.get("state_dir", "/var/lib/agent"))),
        auto_apply=bool(raw.get("auto_apply", True)),
        opa_url=str(_required(_section(raw, "policy"), "opa_url", "policy")),
        opa_timeout=float(_section(raw, "policy").get("timeout", 5.0)),
        plan=dict(plan),
        declared_evidence={k: dict(v) for k, v in declared.items()},
        smoke=_smoke_settings(smoke),
        openbao=openbao,
        codex=CodexSettings(**dict(raw.get("codex") or {})),
        probe=ProbeSettings(**dict(raw.get("probe") or {})),
        restart=RestartSettings(**dict(raw.get("restart") or {})),
        slack_channel=(raw.get("slack") or {}).get("channel"),
        slack_thread_ts=_thread_ts((raw.get("slack") or {}).get("thread_ts")),
        approve_command=str(raw.get("approve_command") or "python -m agent approve"),
        platform_url=(raw.get("platform") or {}).get("url") or None,
        platform_agent_id=str((raw.get("platform") or {}).get("agent_id") or "oneport-agent"),
        platform_token_env=str((raw.get("platform") or {}).get("token_env") or "ONEPORT_INGEST_TOKEN"),
    )


def load_config(path: str | None = None, env: Mapping[str, str] | None = None) -> AgentConfig:
    environ = os.environ if env is None else env
    resolved = path or environ.get("AGENT_CONFIG") or DEFAULT_CONFIG_PATH
    try:
        with open(resolved, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError(f"설정 파일을 읽지 못했다: {resolved} ({type(exc).__name__})") from None
    if not isinstance(raw, Mapping):
        raise ConfigError(f"설정 파일 형식이 틀렸다: {resolved}")
    try:
        return parse_config(raw, environ)
    except TypeError as exc:  # 설정 절에 모르는 키가 있으면 조용히 무시하지 않는다
        raise ConfigError(f"설정에 모르는 키가 있다: {exc}") from None
