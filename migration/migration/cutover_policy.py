"""Fail-closed OPA gate for the demo cutover executor.

The evidence file is an operator-controlled, secret-free snapshot, not a
permission to execute a command. It is reloaded at each boundary. OPA still
validates freshness and facts. No cached allow is reused.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from . import policy_client as pc


class CutoverPolicyError(RuntimeError):
    pass


def execution_digest(config: Config) -> str:
    # Config contains credential *references*, never resolved passwords.
    # Only the hash is passed to OPA/logs.
    return pc.digest_of(asdict(config))


class CutoverPolicyGate:
    def __init__(self, config: Config, state_dir: Path, *, evidence_file: str | None = None,
                 evaluator: pc.Evaluator | None = None, collector_settings: str | None = None) -> None:
        self.config = config
        self.state_dir = state_dir
        self.evidence_file = evidence_file or os.environ.get("CUTOVER_POLICY_EVIDENCE")
        url = os.environ.get("POLICY_OPA_URL")
        self.evaluator = evaluator or (pc.OpaHttpEvaluator(url) if url else None)
        self.plan_digest: str | None = None
        self.collector_settings = collector_settings or os.environ.get("CUTOVER_POLICY_COLLECTOR_SETTINGS")

    def __call__(self, stage: str, local_checks: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.evidence_file or self.evaluator is None:
            raise CutoverPolicyError("CUTOVER_POLICY_NOT_CONFIGURED")
        if self.collector_settings:
            from .cutover_refresh import refresh
            try:
                refresh(self.config, Path(self.collector_settings), Path(self.evidence_file))
            except Exception:
                raise CutoverPolicyError("CUTOVER_POLICY_REFRESH_FAILED") from None
        try:
            with Path(self.evidence_file).open('rb') as stream:
                raw = stream.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("oversized")
            doc = json.loads(raw)
            plan = dict(doc["plan"])
            if (doc["job_id"] != self.config.job.job_id or
                    plan["source"] != self.config.job.source or
                    plan["target"] != self.config.job.target or
                    plan["execution_digest"] != execution_digest(self.config)):
                raise CutoverPolicyError("CUTOVER_POLICY_EXECUTION_MISMATCH")
            computed = pc.digest_of({k: v for k, v in plan.items() if k != 'digest'})
            if plan.get('digest') != computed:
                raise CutoverPolicyError("CUTOVER_POLICY_PLAN_DIGEST_MISMATCH")
            if self.plan_digest is not None and self.plan_digest != computed:
                raise CutoverPolicyError("CUTOVER_POLICY_PLAN_CHANGED")
            self.plan_digest = computed
            checks = dict(doc['checks'])
            checks.update(local_checks or {})
            now = datetime.now(UTC)
            for check in checks.values():
                observed = datetime.fromisoformat(check['observed_at'].replace('Z', '+00:00'))
                if observed > now:
                    raise CutoverPolicyError("CUTOVER_POLICY_FUTURE_EVIDENCE")
            inp = pc.build_input(job_id=self.config.job.job_id, stage=stage, plan=plan,
                                 checks=checks, now=now)
            # Validate before persistence so a bad collector cannot write secrets to SQLite.
            if pc.validate_input(inp):
                raise CutoverPolicyError("CUTOVER_POLICY_INPUT_INVALID")
        except CutoverPolicyError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise CutoverPolicyError("CUTOVER_POLICY_EVIDENCE_INVALID") from None
        store = pc.DecisionStore(self.state_dir)
        try:
            decision = pc.evaluate(self.evaluator, inp)
            if decision.allowed and (
                    decision.raw_result.get("plan_digest") != plan["digest"] or
                    decision.raw_result.get("evidence_snapshot_id") != inp["evidence"]["snapshot_id"]):
                decision = replace(decision, outcome=pc.ERROR,
                                   reason_codes=(pc.POLICY_RESPONSE_MALFORMED,),
                                   error="CUTOVER_POLICY_RESPONSE_MISMATCH")
            store.record(decision, inp)
        finally:
            store.close()
        if not decision.allowed:
            raise CutoverPolicyError("CUTOVER_POLICY_BLOCKED:" + decision.decision_id + ':' + decision.outcome)
        if (decision.plan_digest != plan['digest'] or
                decision.evidence_snapshot_id != inp['evidence']['snapshot_id']):
            raise CutoverPolicyError("CUTOVER_POLICY_RESPONSE_MISMATCH")
        return {"decision_id": decision.decision_id, "plan_digest": decision.plan_digest,
                "snapshot_id": decision.evidence_snapshot_id, "stage": stage,
                "execution_digest": plan['execution_digest']}
