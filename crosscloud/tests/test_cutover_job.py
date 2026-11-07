import dataclasses
import fcntl

import pytest

from crosscloud import cutover_job

_JOB = "ccx_aws_gcp_20261107153000"


def _save_stage(migration_module, state_dir, stage, **checkpoint):
    state = migration_module("migration.state")
    store = state.CutoverStore(state_dir)
    try:
        store.save(
            state.CutoverJob(
                _JOB, "aws->gcp", "aws", "gcp", stage, checkpoint, ""
            )
        )
    finally:
        store.close()


def test_status_is_not_started_before_migration_cloud_ran(tmp_path):
    status = cutover_job.read_cutover_status(tmp_path, _JOB)

    assert status == cutover_job.CutoverStatus("not_started", None)
    assert not (tmp_path / "cutover.sqlite3").exists()


def test_status_reads_stage_saved_by_migration_cloud(
    tmp_path, migration_module
):
    _save_stage(migration_module, tmp_path, "READY")

    status = cutover_job.read_cutover_status(tmp_path, _JOB)

    assert status == cutover_job.CutoverStatus("READY", None)


def test_status_reads_failed_stage(tmp_path, migration_module):
    _save_stage(
        migration_module,
        tmp_path,
        "FAILED",
        failed_stage="FENCING",
        error_type="CutoverError",
    )

    status = cutover_job.read_cutover_status(tmp_path, _JOB)

    assert status == cutover_job.CutoverStatus("FAILED", "FENCING")


def test_status_of_other_job_is_not_started(tmp_path, migration_module):
    _save_stage(migration_module, tmp_path, "COMPLETED")

    status = cutover_job.read_cutover_status(tmp_path, "ccx_gcp_aws_1")

    assert status.stage == "not_started"


def test_status_refuses_missing_state_dir(tmp_path):
    with pytest.raises(cutover_job.StatusUnreadableError):
        cutover_job.read_cutover_status(tmp_path / "typo", _JOB)


def test_status_refuses_unreadable_store(tmp_path):
    (tmp_path / "cutover.sqlite3").write_text("not sqlite")

    with pytest.raises(cutover_job.StatusUnreadableError):
        cutover_job.read_cutover_status(tmp_path, _JOB)


def test_status_refuses_unknown_stage(tmp_path, migration_module):
    _save_stage(migration_module, tmp_path, "PAUSING")

    with pytest.raises(cutover_job.StatusUnreadableError, match="PAUSING"):
        cutover_job.read_cutover_status(tmp_path, _JOB)


def test_job_lock_refuses_while_migration_cloud_holds_it(tmp_path):
    with (tmp_path / "cloud.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(cutover_job.JobBusyError):
            with cutover_job.migration_job_lock(tmp_path):
                pass


def test_job_lock_blocks_migration_cloud_while_held(tmp_path):
    with cutover_job.migration_job_lock(tmp_path):
        with (tmp_path / "cloud.lock").open("a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    with (tmp_path / "cloud.lock").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_job_lock_refuses_missing_state_dir(tmp_path):
    with pytest.raises(cutover_job.StatusUnreadableError):
        with cutover_job.migration_job_lock(tmp_path / "typo"):
            pass


@pytest.mark.parametrize(
    "stage",
    ["not_started", "PRECHECK", "COPYING", "READY"],
)
def test_routing_change_allowed_before_approval(stage):
    status = cutover_job.CutoverStatus(stage, None)

    assert cutover_job.routing_change_allowed(status, adapter_state=None)


@pytest.mark.parametrize("failed_stage", ["PRECHECK", "COPYING", "READY"])
def test_routing_change_allowed_after_failure_before_approval(failed_stage):
    status = cutover_job.CutoverStatus("FAILED", failed_stage)

    assert cutover_job.routing_change_allowed(status, adapter_state=None)


@pytest.mark.parametrize(
    "stage",
    [
        "FENCING",
        "DRAINING",
        "STOPPING",
        "SWITCHING",
        "COMPLETED",
        "NEEDS_RECOVERY",
    ],
)
def test_routing_change_refused_from_fencing_on(stage):
    status = cutover_job.CutoverStatus(stage, None)

    assert not cutover_job.routing_change_allowed(status, "RUNNING")


def test_routing_change_allowed_when_pause_never_took_effect():
    status = cutover_job.CutoverStatus("FAILED", "FENCING")

    assert cutover_job.routing_change_allowed(status, "RUNNING")


@pytest.mark.parametrize("adapter_state", ["PAUSED", None])
def test_routing_change_refused_when_fencing_failed_after_pause(
    adapter_state,
):
    status = cutover_job.CutoverStatus("FAILED", "FENCING")

    assert not cutover_job.routing_change_allowed(status, adapter_state)


@pytest.mark.parametrize("failed_stage", ["DRAINING", "SWITCHING", None])
def test_routing_change_refused_when_failed_later(failed_stage):
    status = cutover_job.CutoverStatus("FAILED", failed_stage)

    assert not cutover_job.routing_change_allowed(status, "RUNNING")


def test_status_is_frozen():
    status = cutover_job.CutoverStatus("READY", None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        status.stage = "FENCING"
