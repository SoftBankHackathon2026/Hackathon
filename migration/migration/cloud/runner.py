"""Resumable cloud-to-cloud application cutover, fail closed on every error."""

import time
from dataclasses import replace

from ..config import ConfigError
from ..cutover import ApprovalRequired, CutoverError
from ..state import CutoverJob
from .plan import connectors, snapshot_complete

STAGES = (
    "PRECHECK",
    "COPYING",
    "READY",
    "FENCING",
    "DRAINING",
    "STOPPING",
    "SWITCHING",
    "COMPLETED",
)


class Runner:
    def __init__(
        self,
        plan,
        store,
        db,
        connect,
        app,
        *,
        approve=None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.plan, self.store, self.db, self.connect, self.app = (
            plan,
            store,
            db,
            connect,
            app,
        )
        self.approve, self.sleep, self.clock = approve, sleep, clock
        self.c = plan.config
        self.docs = connectors(plan)
        self.job = store.load(self.c.job.job_id)
        if self.job is None:
            self.job = CutoverJob(
                self.c.job.job_id,
                f"{self.c.job.source}->{self.c.job.target}",
                self.c.job.source,
                self.c.job.target,
                "PRECHECK",
                {"manifest": plan.fingerprint},
                "",
            )
            self.save("PRECHECK")
        if self.job.checkpoint.get("manifest") != plan.fingerprint:
            raise ConfigError(
                "saved job manifest differs: use its original configuration"
            )

    def save(self, stage, **evidence):
        self.job = replace(
            self.job, stage=stage, checkpoint={**self.job.checkpoint, **evidence}
        )
        self.store.save(self.job)

    def wait(self, label, predicate):
        deadline = self.clock() + self.c.cutover.stage_timeout_seconds
        while True:
            result = predicate()
            if result:
                return result
            if self.clock() >= deadline:
                raise CutoverError(label + " timed out")
            self.sleep(max(0.01, self.c.cutover.poll_interval_seconds))

    def healthy(self):
        return all(self.connect.is_running(name)[0] for name in self.docs)

    def paused(self, target):
        status = self.app.status()
        if (
            status.get("state") != "PAUSED"
            or status.get("in_flight") != 0
            or status.get("target") != target
        ):
            raise CutoverError(
                "application must acknowledge PAUSED, in_flight=0, and expected target"
            )

    def run(self, *, retry=False):
        if self.job.stage == "NEEDS_RECOVERY":
            raise CutoverError(
                "target may accept writes; manual reconciliation required, no automatic retry/rollback"
            )
        if self.job.stage == "FAILED":
            if not retry:
                raise CutoverError(
                    "inspect failure and use --retry; fences remain installed"
                )
            self.save(self.job.checkpoint["failed_stage"])
        while self.job.stage != "COMPLETED":
            stage = self.job.stage
            try:
                getattr(self, "stage_" + stage.lower())()
            except ApprovalRequired:
                raise
            except Exception as exc:
                # Persist classification only, not DB messages containing customer values.
                self.save(
                    "NEEDS_RECOVERY" if stage == "SWITCHING" else "FAILED",
                    failed_stage=stage,
                    error_type=type(exc).__name__,
                )
                raise CutoverError(
                    f"{stage} failed ({type(exc).__name__}); fences preserved, inspect status"
                ) from None
        return self.job

    def stage_precheck(self):
        result = self.db.precheck()
        status = self.app.status()
        if (
            status.get("target") != self.c.job.source
            or status.get("state") != "RUNNING"
        ):
            raise CutoverError("application must initially run against source")
        if not {self.c.job.source, self.c.job.target}.issubset(
            set(status.get("known_targets", []))
        ):
            raise CutoverError("application adapter must know both targets")
        # Do not attach to an old job/topic/slot implicitly.
        for name in self.docs:
            if self.connect.exists(name):
                raise CutoverError("connector name already exists; new job_id required")
        self.db.fence(self.c.job.target, initial=True)
        self.db.assert_fenced(self.c.job.target)
        self.save("COPYING", precheck=result)

    def stage_copying(self):
        self.db.assert_fenced(self.c.job.target)
        self.db.prepare()
        for name, doc in self.docs.items():
            if self.connect.exists(name):
                actual = self.connect.get_config(name)
                if any(str(actual.get(k)) != str(v) for k, v in doc.items()):
                    raise CutoverError("existing connector config differs")
            self.connect.put_config(name, doc)

        def copied():
            offsets = self.connect.offsets(self.c.job.source_connector)
            return self.healthy() and snapshot_complete(offsets, self.c.job.job_id)

        self.wait("initial snapshot", copied)
        self.save("READY", snapshot_completed=True)

    def stage_ready(self):
        if not self.approve:
            raise ApprovalRequired(
                "READY: --approve <approval-id> required before pausing the application"
            )
        self.save("FENCING", approval=self.approve)

    def stage_fencing(self):
        self.db.assert_fenced(self.c.job.target)
        self.app.pause()
        if self.app.drain(timeout=120).get("settled") is not True:
            raise CutoverError("application drain not settled")
        self.paused(self.c.job.source)
        self.db.fence(self.c.job.source)
        self.db.assert_fenced(self.c.job.source)
        self.save("DRAINING")

    def stage_draining(self):
        self.paused(self.c.job.source)
        for name in (self.c.job.source, self.c.job.target):
            self.db.assert_fenced(name)

        def converged():
            if not self.healthy():
                return False
            report = self.db.verify()
            return report if report["passed"] else False

        report = self.wait("full-table convergence", converged)
        self.save("STOPPING", verify_before_stop=report)

    def stage_stopping(self):
        self.paused(self.c.job.source)
        for name in (self.c.job.source, self.c.job.target):
            self.db.assert_fenced(name)
        for name in self.docs:
            if self.connect.exists(name):
                self.connect.stop(name)

        def stopped():
            for name in self.docs:
                if self.connect.exists(name):
                    status = self.connect.status(name)
                    if status.get("connector", {}).get(
                        "state"
                    ) != "STOPPED" or status.get("tasks"):
                        return False
            return True

        self.wait("connector STOPPED (Connect 3.5+ required)", stopped)
        # No connector is allowed to alter a row after this comparison.
        report = self.db.verify()
        if not report["passed"]:
            raise CutoverError("post-stop full-table comparison failed")
        self.db.sync_sequences()
        for name in self.docs:
            self.connect.delete(name)
        self.save("SWITCHING", verify_after_stop=report)

    def stage_switching(self):
        self.db.assert_fenced(self.c.job.source)
        self.paused(self.c.job.source)
        # Adapter switches connection/routing while still paused. It must not write
        # probe rows or resume automatically. Target fence remains in place here.
        self.db.assert_fenced(self.c.job.target)
        self.app.switch_target(self.c.job.target)
        self.paused(self.c.job.target)
        self.db.release_target()
        self.app.resume()
        status = self.app.status()
        health = self.app.healthz()
        if (
            status.get("state") != "RUNNING"
            or status.get("target") != self.c.job.target
            or health.get("healthy") is not True
            or health.get("target") != self.c.job.target
        ):
            raise CutoverError("target application health acknowledgement failed")
        self.db.assert_fenced(self.c.job.source)
        self.save(
            "COMPLETED",
            application_healthy=True,
            source_fenced=True,
            cleanup="source slot/publication retained: remove after recovery window; monitor retained WAL",
        )
