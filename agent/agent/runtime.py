from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import AgentConfig
from .notify import WEBHOOK_PREFIX, StdoutNotifier, build_notifier
from .scope import allowed_keys, planned_values

log = logging.getLogger("agent.runtime")
SETTINGS_FILE = "settings.json"
THREAD_TS_RE = re.compile(r"\d{6,}\.\d{1,9}")
MIN_INTERVAL, MAX_INTERVAL = 30, 3600


class SettingsError(ValueError):
    pass


def validate(changes: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(changes, Mapping) or not changes:
        raise SettingsError("바꿀 설정이 없어요")
    unknown = sorted(set(changes) - {"auto_apply", "interval_seconds", "slack_enabled", "slack_thread_ts"})
    if unknown:
        raise SettingsError(f"바꿀 수 없는 설정: {', '.join(unknown)}")
    clean: dict[str, Any] = {}
    for key in ("auto_apply", "slack_enabled"):
        if key in changes:
            if not isinstance(changes[key], bool):
                raise SettingsError(f"{key} 는 true/false 여야 해요")
            clean[key] = changes[key]
    if "interval_seconds" in changes:
        value = changes["interval_seconds"]
        if isinstance(value, bool) or not isinstance(value, int) or not MIN_INTERVAL <= value <= MAX_INTERVAL:
            raise SettingsError(f"interval_seconds 는 {MIN_INTERVAL}~{MAX_INTERVAL} 사이 정수여야 해요")
        clean["interval_seconds"] = value
    if "slack_thread_ts" in changes:
        value = changes["slack_thread_ts"]
        if not isinstance(value, str) or (value and not THREAD_TS_RE.fullmatch(value)):
            raise SettingsError("slack_thread_ts 는 비우거나 1700000000.000100 같은 값이어야 해요")
        clean["slack_thread_ts"] = value
    return clean


class AgentRuntime:
    def __init__(
        self, cfg: AgentConfig, remediator: Any, state_dir: Path | str, *, interval: float,
        env: Mapping[str, str] | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.cfg, self.remediator = cfg, remediator
        self.env = os.environ if env is None else env
        self.now = now
        self.path = Path(state_dir) / SETTINGS_FILE
        self.interval = float(interval)
        self.slack_enabled = True
        self.slack_thread_ts = cfg.slack_thread_ts or ""
        self.applied_at: str | None = None
        self._load()
        self._rebuild_notifier()

    def _load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("저장된 설정을 읽지 못해 기본 설정으로 시작한다: %s", self.path)
            return
        try:
            self._set(validate({k: v for k, v in saved.items() if k != "applied_at"}))
        except SettingsError as exc:
            log.warning("저장된 설정이 맞지 않아 무시한다: %s", exc)
            return
        self.applied_at = saved.get("applied_at") if isinstance(saved.get("applied_at"), str) else None

    def _set(self, clean: Mapping[str, Any]) -> None:
        if "auto_apply" in clean:
            self.remediator.auto_apply = clean["auto_apply"]
        if "interval_seconds" in clean:
            self.interval = float(clean["interval_seconds"])
        if "slack_enabled" in clean:
            self.slack_enabled = clean["slack_enabled"]
        if "slack_thread_ts" in clean:
            self.slack_thread_ts = clean["slack_thread_ts"]

    def _rebuild_notifier(self) -> None:
        if self.slack_enabled:
            notifier = build_notifier(self.cfg.slack_channel, self.env, thread_ts=self.slack_thread_ts or None)
        else:
            notifier = StdoutNotifier()
        self.remediator.d.notifier = notifier

    def _saved(self) -> dict[str, Any]:
        return {
            "auto_apply": bool(self.remediator.auto_apply), "interval_seconds": int(self.interval),
            "slack_enabled": self.slack_enabled, "slack_thread_ts": self.slack_thread_ts, "applied_at": self.applied_at,
        }

    def apply(self, changes: Mapping[str, Any]) -> str:
        clean = validate(changes)
        self._set(clean)
        self._rebuild_notifier()
        self.applied_at = self.now().isoformat(timespec="seconds").replace("+00:00", "Z")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            pending = self.path.with_name(self.path.name + ".tmp")
            pending.write_text(json.dumps(self._saved(), ensure_ascii=False), encoding="utf-8")
            pending.replace(self.path)
        except OSError as exc:
            log.warning("설정을 저장하지 못했다(재시작하면 되돌아간다): %s", type(exc).__name__)
        return "반영함: " + ", ".join(f"{k}={v}" for k, v in clean.items())

    def slack_mode(self) -> str:
        if str(self.env.get("SLACK_WEBHOOK_URL", "")).startswith(WEBHOOK_PREFIX):
            return "webhook"
        if self.env.get("SLACK_BOT_TOKEN") and self.cfg.slack_channel:
            return "bot"
        return "none"

    def snapshot(self) -> dict[str, Any]:
        codex = self.cfg.codex
        return {
            "auto_apply": bool(self.remediator.auto_apply),
            "interval_seconds": int(self.interval),
            "job_id": self.cfg.job_id,
            "stage": self.cfg.smoke.stage,
            "slack": {
                "mode": self.slack_mode(), "enabled": self.slack_enabled,
                "channel": self.cfg.slack_channel or None, "thread_ts": self.slack_thread_ts or None,
                "buttons": self.cfg.slack_buttons,
            },
            "fix": {"keys": allowed_keys(self.cfg.target_db), "plan": planned_values(self.cfg.target_db)},
            "sandbox": {
                "runner": codex.runner,
                "image": codex.sandbox_image if codex.runner == "opensandbox" else None,
                "model": codex.model,
            },
            "settings_applied_at": self.applied_at,
        }
