"""Authenticated receipt-ledger handoff. Payloads are streamed page by page, never checkpointed."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from .config import Config


class HandoffError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # An old origin redirecting/proxying to AWS is not proof it can be retired.
        return None


class ReceiptHandoff:
    def __init__(self, config: Config, *, env: dict[str, str] | None = None) -> None:
        assert config.full_migration is not None
        self.config = config
        self.full = config.full_migration
        self.id = config.job.job_id
        env = os.environ if env is None else env
        names = {"source": config.ingress.token_env,
                 "destination": self.full.destination_token_env,
                 "receipt": self.full.destination_api_token_env,
                 "app": config.ingress.app_checks[config.job.target].token_env}
        self.tokens = {name: env.get(key or "", "") for name, key in names.items()}
        if any(not self.tokens[name] for name in ("source", "destination", "receipt")):
            raise HandoffError("full migration requires source/destination/public API tokens")
        self.urls = {"source": (config.ingress.control_url or "").rstrip("/"),
                     "destination": self.full.destination_control_url.rstrip("/"),
                     "receipt": self.full.destination_receipt_url.rstrip("/"),
                     "app": config.ingress.app_checks[config.job.target].url.rstrip("/")}
        self.opener = urllib.request.build_opener(_NoRedirect())

    def _call(self, side: str, method: str, path: str, body: dict | None = None) -> dict:
        # Bounded wire envelope. A source page is <=1 MiB; leave room for metadata.
        limit = 2 * 1024 * 1024
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        if data is not None and len(data) > limit:
            raise HandoffError("receipt handoff page exceeds transport limit")
        req = urllib.request.Request(self.urls[side] + path, method=method, data=data,
                                     headers={"Authorization": "Bearer " + self.tokens[side],
                                              "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(req, timeout=self.full.timeout_seconds) as response:
                raw = response.read(limit + 1)
                if len(raw) > limit:
                    raise HandoffError("receipt handoff response exceeds transport limit")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise HandoffError("invalid receipt handoff response")
                return result
        except urllib.error.HTTPError as exc:
            # Never include response content: export/import may contain customer records.
            raise HandoffError(f"{side} handoff HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise HandoffError(f"{side} handoff request failed") from None

    def precheck(self) -> dict:
        source = self._call("source", "GET", "/handoff/status")
        dest = self._call("destination", "GET", "/handoff/status")
        source_id, dest_id = source.get("ledger_id"), dest.get("ledger_id")
        if not source_id or not dest_id or source_id == dest_id:
            raise HandoffError("receipt source/destination ledger identities must differ")
        if source.get("admission") != "open" or source.get("handoff_id"):
            raise HandoffError("source receipt ledger is not available for a new handoff")
        if dest.get("admission") != "standby" or dest.get("handoff_id"):
            raise HandoffError("destination receipt must be an unused standby ledger")
        self._check_app_identity(writes_open=False)
        return {"source_ledger_id": source_id, "destination_ledger_id": dest_id}

    def seal(self, binding: dict) -> dict:
        source = self._call("source", "GET", "/handoff/status")
        dest = self._call("destination", "GET", "/handoff/status")
        if (source.get("ledger_id") != binding["source_ledger_id"]
                or dest.get("ledger_id") != binding["destination_ledger_id"]):
            raise HandoffError("receipt ledger changed since precheck")
        result = self._call("source", "POST", "/handoff/seal",
                            {"id": self.id, "destination_ledger_id": binding["destination_ledger_id"]})
        manifest = result.get("manifest")
        if (not isinstance(manifest, dict) or type(manifest.get("count")) is not int
                or not 0 <= manifest["count"] <= 100000 or not manifest.get("digest")):
            raise HandoffError("invalid sealed receipt manifest")
        return {"id": self.id, **binding, "manifest": manifest}

    def transfer(self, sealed: dict) -> dict:
        cursor = "0-0"
        count = 0
        while True:
            page = self._call("source", "GET", "/handoff/export?" + urlencode({"id": self.id, "cursor": cursor}))
            if any(page.get(key) != sealed[key] for key in ("id", "source_ledger_id", "destination_ledger_id", "manifest")):
                raise HandoffError("receipt export does not match the sealed handoff")
            records = page.get("records")
            if not isinstance(records, list) or len(records) > 100 or page.get("cursor") != cursor:
                raise HandoffError("invalid receipt export page")
            count += len(records)
            if count > sealed["manifest"]["count"]:
                raise HandoffError("receipt export exceeds manifest count")
            self._call("destination", "POST", "/handoff/import", page)
            if page.get("done") is True:
                break
            next_cursor = page.get("next_cursor")
            if not records or not isinstance(next_cursor, str) or next_cursor == cursor:
                raise HandoffError("receipt export made no progress")
            cursor = next_cursor
        dest = self._call("destination", "GET", "/handoff/status")
        if (count != sealed["manifest"]["count"]
                or dest.get("imported_count") != count
                or dest.get("imported_digest") != sealed["manifest"]["digest"]
                or dest.get("ledger_id") != sealed["destination_ledger_id"]
                or dest.get("handoff_id") != self.id):
            raise HandoffError("receipt import verification failed")
        return {"count": count, "digest": sealed["manifest"]["digest"]}

    def activate(self, sealed: dict) -> dict:
        self._call("destination", "POST", "/handoff/activate", {**sealed, "target": self.full.destination_target})
        return self.resume_destination(sealed)

    def resume_destination(self, sealed: dict) -> dict:
        handoff = self._call("destination", "GET", "/handoff/status")
        if (handoff.get("ledger_id") != sealed["destination_ledger_id"]
                or handoff.get("handoff_id") != self.id or handoff.get("state") != "active"
                or handoff.get("admission") != "open"):
            raise HandoffError("destination receipt identity/activation mismatch")
        state = self._call("destination", "GET", "/status")
        if state.get("target") != self.full.destination_target:
            raise HandoffError("destination receipt points at a different app")
        if state.get("mode") == "paused":
            self._call("destination", "POST", "/resume", {"expected_epoch": state.get("epoch")})
            state = self._call("destination", "GET", "/status")
        if state.get("mode") != "running":
            raise HandoffError("destination receipt delivery is not running")
        return {"ledger_id": handoff["ledger_id"], "mode": state["mode"], "target": state["target"]}

    def forward(self, sealed: dict) -> dict:
        self._call("source", "POST", "/handoff/forward", {"id": self.id, "target": self.full.forward_target})
        state = self._call("source", "GET", "/handoff/status")
        if (state.get("ledger_id") != sealed["source_ledger_id"]
                or state.get("handoff_id") != self.id or state.get("admission") != "forwarding"):
            raise HandoffError("source receipt forwarding was not persisted")
        return {"ledger_id": state["ledger_id"], "admission": state["admission"]}

    def verify_destination(self, sealed: dict) -> dict:
        receipt = self._call("receipt", "GET", "/api/receipt")
        if (receipt.get("ledger_id") != sealed["destination_ledger_id"]
                or receipt.get("handoff_id") != self.id or receipt.get("admission") != "open"):
            raise HandoffError("destination receipt reaches the old or an unrelated ledger")
        info = self._check_app_identity(writes_open=True)
        return {"receipt_ledger_id": receipt["ledger_id"], "handoff_id": self.id,
                "app_environment": info["environment"], "database": info["database"]["name"],
                "app_version": info["version"]}

    def _check_app_identity(self, *, writes_open: bool) -> dict:
        # App identity is readable before DB GRANT; /readyz may correctly fail
        # until target INSERT permission is opened in SWITCHING.
        info = self._call("app", "GET", "/api/info")
        expected = self.config.ingress.app_checks[self.config.job.target]
        if (info.get("environment") != expected.environment
                or (info.get("database") or {}).get("name") != expected.database
                or info.get("version") != self.full.destination_app_version):
            raise HandoffError("destination app artifact/database/environment mismatch")
        if writes_open:
            ready = self._call("app", "GET", "/readyz")
            if ready.get("status") != "ok" or info.get("writes_paused") is not False:
                raise HandoffError("destination app is not ready for writes")
        return info
