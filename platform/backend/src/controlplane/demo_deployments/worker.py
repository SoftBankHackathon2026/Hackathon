"""Orchestration only. CodeBuild and each cloud worker retain their durable journals."""

import logging
import time

from controlplane.builds.models import BuildInput, BuildSettings, BuildState
from controlplane.builds.store import BuildStore
from controlplane.core import db
from controlplane.core.errors import RequestConflict
from controlplane.demo_deployments.models import RunSettings
from controlplane.demo_deployments.service import RunService
from controlplane.demo_deployments.snapshots import RunSnapshot
from controlplane.demo_deployments.states import (
    RELEASE_STATES_KEPT,
    SETTLED_TARGET_STATES,
    RunState,
    TargetState,
    aggregate_run_state,
)
from controlplane.demo_deployments.store import RunStore
from controlplane.providers.interface import DeploymentSpec
from controlplane.schemas.accounts import Identity

LOG = logging.getLogger(__name__)


BUILD_KEY_PREFIX = "run-build-"


def image_source(image):
    return {"kind": "image", "image": image}


class RunWorker:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.builds = BuildStore(self.store.dsn)

    @staticmethod
    def build_request(row, snapshot: RunSnapshot | None = None):
        snapshot = snapshot or RunSnapshot.model_validate(row["snapshot"])
        return BuildInput(
            **{**snapshot.source, "revision": snapshot.commit},
            deployment=DeploymentSpec.model_validate(snapshot.targets[0].spec),
            deploy_after_build=False,
            idempotency_key=BUILD_KEY_PREFIX + row["id"],
        )

    def tick(self):
        with self.store.run_lock() as conn:
            if conn is None:
                return
            for run_id in self.store.active_run_ids(conn):
                row = self.store.get(run_id)
                try:
                    self.step(conn, row)
                except Exception:
                    conn.rollback()
                    self.store.update(
                        conn, row["id"], RunState.UNCERTAIN, error_code="run_observation_required"
                    )

    def step(self, conn, row):
        snapshot = RunSnapshot.model_validate(row["snapshot"])
        identity = (
            Identity(email=row["created_by"], team=row["team"], role="member")
            if row["team"]
            else None
        )
        if row["state"] == RunState.QUEUED:
            self._enqueue_build(conn, row, snapshot, identity)
        elif not row["image"]:
            self._recover_build(conn, row, snapshot)
        else:
            self._observe_targets(conn, row, snapshot, identity)

    def _skip_targets(self, conn, row, error_code):
        for target in row["targets"]:
            self.store.target(
                conn, row["id"], target["runtime"], TargetState.SKIPPED, error_code=error_code
            )

    def _enqueue_build(self, conn, row, snapshot, identity):
        run_id = row["id"]
        try:
            self.service.validate(snapshot, identity, check_history=True)
            build = self.builds.enqueue(
                self.build_request(row, snapshot),
                self.service.build_settings.max_pending,
                self.service.build_settings.max_daily,
                team=row["team"],
                created_by=row["created_by"],
            )
        except (RequestConflict, ValueError):
            self._skip_targets(conn, row, "build_intake_rejected")
            self.store.update(conn, run_id, RunState.FAILED, error_code="build_intake_rejected")
            return
        self.store.update(conn, run_id, RunState.BUILDING, build_id=build["id"])

    def _find_build(self, run_id):
        with self.builds.connect() as build_conn:
            return build_conn.execute(
                "SELECT * FROM source_builds WHERE idempotency_key=%s", (BUILD_KEY_PREFIX + run_id,)
            ).fetchone()

    def _build_mismatch(self, row, snapshot, build) -> bool:
        commit = build["checkpoint"].get("commit")
        return (
            build["team"] != row["team"]
            or build["request"] != self.build_request(row, snapshot).model_dump(mode="json")
            or (row["build_id"] is not None and row["build_id"] != build["id"])
            or (commit is not None and commit != snapshot.commit)
            or (build["state"] == BuildState.SUCCEEDED and commit != snapshot.commit)
        )

    def _recover_build(self, conn, row, snapshot):
        run_id = row["id"]
        build = self._find_build(run_id)
        if not build:
            self.store.update(conn, run_id, RunState.UNCERTAIN, error_code="build_handle_missing")
        elif self._build_mismatch(row, snapshot, build):
            self.store.update(
                conn, run_id, RunState.UNCERTAIN, error_code="build_identity_mismatch"
            )
        elif build["state"] == BuildState.SUCCEEDED and build["image"]:
            self.store.update(
                conn, run_id, RunState.DEPLOYING, build_id=build["id"], image=build["image"]
            )
        elif build["state"] == BuildState.FAILED:
            self._skip_targets(conn, row, build["error_code"])
            self.store.update(
                conn, run_id, RunState.FAILED, build_id=build["id"], error_code=build["error_code"]
            )
        else:
            state = (
                RunState.UNCERTAIN if build["state"] == BuildState.UNCERTAIN else RunState.BUILDING
            )
            self.store.update(
                conn, run_id, state, build_id=build["id"], error_code=build["error_code"]
            )

    def _observe_targets(self, conn, row, snapshot, identity):
        run_id = row["id"]
        for target in row["targets"]:
            if target["state"] not in SETTLED_TARGET_STATES:
                self._observe_target(conn, row, snapshot, identity, target)
        latest = self.store.get(run_id)
        self.store.update(
            conn, run_id, aggregate_run_state([t["state"] for t in latest["targets"]])
        )

    def _child_release(self, conn, key):
        child = self.store.release_by_key(conn, key)
        conn.commit()
        return child

    def _observe_target(self, conn, row, snapshot, identity, target):
        run_id, runtime = row["id"], target["runtime"]
        frozen = snapshot.target(runtime)
        key = f"run-{run_id}-{runtime}-{target['attempt']}"
        child = self._child_release(conn, key)
        if not child and target["state"] == TargetState.PENDING:
            child = self._submit_child(conn, row, snapshot, identity, target, key)
            if child is None:
                return
        if not child:
            self.store.target(
                conn, run_id, runtime, TargetState.UNCERTAIN, error_code="release_handle_missing"
            )
            return
        if self._release_mismatch(row, frozen, child):
            self.store.target(
                conn, run_id, runtime, TargetState.UNCERTAIN, error_code="release_identity_mismatch"
            )
            return
        state = child["state"] if child["state"] in RELEASE_STATES_KEPT else TargetState.RUNNING
        self.store.target(
            conn,
            run_id,
            runtime,
            state,
            release_id=child["id"],
            error_code=child["error_code"],
            service_url=child["service_url"],
        )

    def _submit_child(self, conn, row, snapshot, identity, target, key):
        run_id, runtime = row["id"], target["runtime"]
        frozen = snapshot.target(runtime)
        try:
            latest = self.store.latest_release(conn, target["environment_id"])
            conn.commit()
            known = {a["release_id"] for a in target["attempts"]}
            known.add(frozen.last_release_id)
            if latest and latest["id"] not in known:
                raise RequestConflict("시연 접수 이후 다른 배포가 있습니다.")
            self.service.validate(snapshot, identity, runtimes=[runtime])
            payload = {**frozen.spec, "source": image_source(row["image"])}
            self.service.deployments.submit(DeploymentSpec.model_validate(payload), key, identity)
        except Exception as exc:
            conn.rollback()
            child = self._child_release(conn, key)
            if not child:
                known_failure = isinstance(exc, (RequestConflict, ValueError))
                self.store.target(
                    conn,
                    run_id,
                    runtime,
                    TargetState.FAILED if known_failure else TargetState.UNCERTAIN,
                    error_code="deployment_rejected"
                    if known_failure
                    else "submission_observation_required",
                )
                return None
            return child
        return self._child_release(conn, key)

    @staticmethod
    def _release_mismatch(row, frozen, child) -> bool:
        expected = frozen.spec
        if child["request"] != {**frozen.request, "source": image_source(row["image"])}:
            return True
        return (
            child["application"],
            child["environment_id"],
            child["runtime"],
            child["team"],
            child["binding_fingerprint"],
            child["request"]["source"].get("image"),
        ) != (
            expected["application"],
            expected["environment_id"],
            frozen.runtime,
            row["team"],
            frozen.binding_fingerprint,
            row["image"],
        )


def main():
    from controlplane.providers.loader import configured
    from controlplane.services.deployments import DeploymentService

    settings, build_settings = RunSettings.from_environment(), BuildSettings.from_environment()
    if not settings or not build_settings:
        raise SystemExit("ONEPORT_DEMO_DEPLOY_CONFIG and ONEPORT_BUILD_CONFIG are required")
    registry, releases, _ = configured(require_token=False)
    if not releases:
        raise SystemExit("ONEPORT_CONFIG is required")
    releases.migrate()
    registry.initialize()
    BuildStore(releases.dsn).migrate()
    RunStore(releases.dsn).migrate()
    worker = RunWorker(RunService(settings, build_settings, DeploymentService(registry, releases)))
    logging.basicConfig(level=logging.INFO)
    with db.pooling():
        while True:
            try:
                worker.tick()
            except Exception as exc:
                LOG.error("deployment run worker unavailable: %s", type(exc).__name__)
            time.sleep(settings.poll_seconds)


if __name__ == "__main__":
    main()
