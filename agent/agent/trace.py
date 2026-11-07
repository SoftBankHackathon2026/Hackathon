"""샌드박스 안에서 Codex 가 한 일을 화면에 보여 줄 만큼만 줄여 남긴다.

`codex exec --json` 의 JSONL 이벤트에서 생각(reasoning), 실행한 명령, 바꾼 파일만 뽑는다.
명령 출력과 모델 최종 응답은 남기지 않는다. 최종 응답은 제안으로 따로 검사해 남긴다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

MAX_STEPS = 40
MAX_TEXT = 400
MAX_FILE = 4096

THINK, SAY, RUN, EDIT = "think", "say", "run", "edit"
_SHELL_RE = re.compile(r"^(?:/(?:usr/)?bin/)?(?:ba)?sh -l?c (['\"])(.*)\1$", re.DOTALL)
WORKSPACE_MARK = "/workspace/"


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _item(event: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    details = item.get("details")
    return {**item, **details} if isinstance(details, dict) else item


def _command(value: Any) -> str:
    if isinstance(value, list) and len(value) == 3 and str(value[0]).endswith("sh") and value[1] in ("-c", "-lc"):
        return str(value[2])
    text = " ".join(str(part) for part in value) if isinstance(value, list) else str(value or "")
    inner = _SHELL_RE.match(text.strip())
    return inner.group(2) if inner else text


def _path(value: Any) -> str:
    text = str(value)
    return text.split(WORKSPACE_MARK, 1)[1] if WORKSPACE_MARK in text else text


def _step(item: dict[str, Any]) -> dict[str, Any] | None:
    kind = item.get("type") or item.get("item_type")
    if kind == "reasoning":
        text = str(item.get("text") or "").replace("**", "").strip()
        return {"kind": THINK, "text": text} if text else None
    if kind == "agent_message":
        text = str(item.get("text") or "").strip()
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            text = " ".join(str(doc[k]) for k in ("cause", "impact") if isinstance(doc.get(k), str) and doc[k].strip())
        return {"kind": SAY, "text": text} if text else None
    if kind == "command_execution":
        command = _command(item.get("command")).strip()
        if not command:
            return None
        code = item.get("exit_code")
        return {"kind": RUN, "text": command, "ok": code == 0 if isinstance(code, int) else None}
    if kind == "file_change":
        changes = item.get("changes")
        paths = [_path(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")] if isinstance(changes, list) else []
        return {"kind": EDIT, "text": ", ".join(paths)} if paths else None
    return None


def _events(text: str) -> list[Any]:
    decoder, events, at = json.JSONDecoder(), [], 0
    while True:
        start = text.find("{", at)
        if start < 0:
            return events
        try:
            event, at = decoder.raw_decode(text, start)
        except ValueError:
            at = start + 1
            continue
        events.append(event)


def parse_trace(text: str, clean: Callable[[str], str] = lambda s: s) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for event in _events(text):
        item = _item(event) if isinstance(event, dict) else None
        step = _step(item) if item else None
        if step is None:
            continue
        step["text"] = _clip(clean(step["text"]))
        steps.append(step)
    said = [i for i, s in enumerate(steps) if s["kind"] == SAY]
    if said:
        del steps[said[-1]]
    if len(steps) > MAX_STEPS:
        steps = [*steps[: MAX_STEPS - 1], {"kind": THINK, "text": f"이후 {len(steps) - MAX_STEPS + 1}단계는 생략했어요."}]
    return steps


def file_record(path: str, before: str, after: str | None) -> dict[str, Any]:
    return {"path": path, "before": before[:MAX_FILE], "after": None if after is None else after[:MAX_FILE]}
