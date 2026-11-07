"""에이전트 설정. YAML 에는 비밀값을 적지 않는다. 토큰·키는 환경변수로만 받는다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from migration.policy_client import digest_of


class ConfigError(ValueError):
    """설정 파일·환경변수 오류."""


STORE_KINDS: tuple[str, ...] = ("file", "vault", "aws-secrets-manager")


@dataclass(frozen=True)
class StoreConfig:
    kind: str
    # file
    path: str | None = None
    # vault (토큰은 VAULT_TOKEN)
    addr: str | None = None
    mount: str = "secret"
    secret_path: str | None = None
    # aws-secrets-manager (자격증명은 표준 AWS 환경변수/IRSA)
    secret_id: str | None = None
    region: str | None = None


@dataclass(frozen=True)
class ScopeConfig:
    """AI 가 바꿀 수 있는 범위. allowed_values 는 계획에서 채운다."""

    allowed_keys: tuple[str, ...]
    secret_keys: tuple[str, ...] = ("DB_PASSWORD",)
    host_key: str = "DB_HOST"
    port_key: str = "DB_PORT"
    dbname_key: str = "DB_NAME"
    user_key: str = "DB_USER"
    password_key: str = "DB_PASSWORD"
    sslmode_key: str = "DB_SSLMODE"
    table: str = "orders"


@dataclass(frozen=True)
class CodexConfig:
    bin: str = "codex"
    model: str | None = None
    timeout_seconds: float = 300.0
    max_attempts: int = 2
    # 환경변수 이름. 값은 설정에 쓰지 않는다.
    api_key_env: str = "CODEX_API_KEY"
    # CODEX_HOME. None 이면 실행마다 빈 임시 디렉터리(인증은 CODEX_API_KEY 환경변수). 로컬 개발에서만 ~/.codex 를 지정한다.
    home: str | None = None


@dataclass(frozen=True)
class OpaConfig:
    url: str | None = None
    policy_dir: str | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class SlackConfig:
    channel: str | None = None
    bot_token_env: str = "SLACK_BOT_TOKEN"
    app_token_env: str = "SLACK_APP_TOKEN"


@dataclass(frozen=True)
class AgentConfig:
    job_id: str
    target_env: str
    store: StoreConfig
    scope: ScopeConfig
    plan: Mapping[str, Any]
    codex: CodexConfig
    opa: OpaConfig
    slack: SlackConfig
    state_dir: Path
    evidence_extra_path: Path | None = None
    probe_timeout_seconds: float = 5.0
    stage: str = "pre_cutover"
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def extra_checks(self) -> dict[str, Any]:
        """다른 수집기(SEC-02)가 만든 검사 결과 파일. 없으면 빈 dict (정책이 EVIDENCE-MISSING 으로 보류한다)."""
        if self.evidence_extra_path is None or not self.evidence_extra_path.is_file():
            return {}
        with self.evidence_extra_path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, Mapping):
            raise ConfigError("evidence_extra 파일은 {검사이름: check} 객체여야 한다")
        return dict(data)


def _req(m: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in m or m[key] in (None, ""):
        raise ConfigError(f"{where}.{key} 가 없다")
    return m[key]


def parse_config(raw: Mapping[str, Any], *, base_dir: Path | None = None) -> AgentConfig:
    base = base_dir or Path.cwd()

    def rel(p: str | None) -> Path | None:
        if p is None:
            return None
        path = Path(p)
        return path if path.is_absolute() else base / path

    job_id = str(_req(raw, "job_id", "config"))
    target_env = str(raw.get("target_env", "aws"))

    s = raw.get("store") or {}
    kind = str(_req(s, "kind", "store"))
    if kind not in STORE_KINDS:
        raise ConfigError(f"store.kind 는 {STORE_KINDS} 중 하나다: {kind!r}")
    sub = s.get(kind.replace("-", "_")) or s.get(kind) or {}
    if kind == "file":
        store = StoreConfig(kind, path=str(rel(_req(sub, "path", "store.file"))))
    elif kind == "vault":
        store = StoreConfig(kind, addr=str(_req(sub, "addr", "store.vault")), mount=str(sub.get("mount", "secret")),
                            secret_path=str(_req(sub, "path", "store.vault")))
    else:
        store = StoreConfig(kind, secret_id=str(_req(sub, "secret_id", "store.aws")), region=sub.get("region"))

    sc = raw.get("scope") or {}
    allowed = sc.get("allowed_keys") or ["DB_HOST", "DB_PORT"]
    if not isinstance(allowed, list) or not all(isinstance(k, str) and k for k in allowed):
        raise ConfigError("scope.allowed_keys 는 문자열 배열이어야 한다")
    scope = ScopeConfig(
        allowed_keys=tuple(allowed),
        secret_keys=tuple(sc.get("secret_keys") or ["DB_PASSWORD"]),
        host_key=sc.get("host_key", "DB_HOST"), port_key=sc.get("port_key", "DB_PORT"),
        dbname_key=sc.get("dbname_key", "DB_NAME"), user_key=sc.get("user_key", "DB_USER"),
        password_key=sc.get("password_key", "DB_PASSWORD"), sslmode_key=sc.get("sslmode_key", "DB_SSLMODE"),
        table=str(sc.get("table", "orders")),
    )

    plan = dict(raw.get("plan") or {})
    if not isinstance(plan.get("limits"), Mapping) or not plan["limits"].get("evidence_ttl_seconds"):
        raise ConfigError("plan.limits.evidence_ttl_seconds 가 필요하다")
    if not isinstance(plan.get("target_db"), Mapping) or not plan["target_db"].get("host"):
        raise ConfigError("plan.target_db.host 가 필요하다 (AI 가 쓸 수 있는 유일한 대상 주소)")
    plan.setdefault("remediation", {"auto_apply": False})
    if "digest" not in plan:
        plan["digest"] = digest_of({k: v for k, v in plan.items() if k != "digest"})

    cx = raw.get("codex") or {}
    codex = CodexConfig(bin=str(cx.get("bin", "codex")), model=cx.get("model"),
                        timeout_seconds=float(cx.get("timeout_seconds", 300)),
                        max_attempts=int(cx.get("max_attempts", 2)), api_key_env=str(cx.get("api_key_env", "CODEX_API_KEY")),
                        home=str(Path(cx["home"]).expanduser()) if cx.get("home") else None)

    op = raw.get("opa") or {}
    opa = OpaConfig(url=op.get("url"), policy_dir=str(rel(op.get("policy_dir"))) if op.get("policy_dir") else None,
                    timeout_seconds=float(op.get("timeout_seconds", 5)))
    if not opa.url and not opa.policy_dir:
        raise ConfigError("opa.url 또는 opa.policy_dir 중 하나가 필요하다")

    sl = raw.get("slack") or {}
    slack = SlackConfig(channel=sl.get("channel"), bot_token_env=str(sl.get("bot_token_env", "SLACK_BOT_TOKEN")),
                        app_token_env=str(sl.get("app_token_env", "SLACK_APP_TOKEN")))

    state_dir = rel(str(raw.get("state_dir", "state"))) or base / "state"
    return AgentConfig(
        job_id=job_id, target_env=target_env, store=store, scope=scope, plan=plan, codex=codex, opa=opa, slack=slack,
        state_dir=state_dir, evidence_extra_path=rel(raw.get("evidence_extra")),
        probe_timeout_seconds=float(raw.get("probe_timeout_seconds", 5)), stage=str(raw.get("stage", "pre_cutover")),
        raw=dict(raw),
    )


def load_config(path: str | Path) -> AgentConfig:
    p = Path(path)
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError("설정 파일 최상위는 객체여야 한다")
    return parse_config(raw, base_dir=p.resolve().parent)


def env_secret(name: str) -> str | None:
    """환경변수에서 비밀값을 읽는다. 값은 호출자가 로그에 남기지 않는다."""
    v = os.environ.get(name)
    return v.strip() if v else None
