import fcntl
import sqlite3

import pytest

from crosscloud import cutover_stage

_JOB = "ccx_aws_gcp_20261107153000"


def _store(migration_module, state_dir):
    return migration_module("migration.state").CutoverStore(state_dir)


def _save(migration_module, store, stage, **checkpoint):
    state = migration_module("migration.state")
    store.save(
        state.CutoverJob(_JOB, "aws->gcp", "aws", "gcp", stage, checkpoint, "")
    )


def test_missing_state_file_is_its_own_error(tmp_path):
    with pytest.raises(cutover_stage.NoCutoverStateError):
        cutover_stage.read_cutover_stage(_JOB, tmp_path)
    assert not (tmp_path / "cutover.sqlite3").exists()


def test_missing_state_dir_is_unreadable(tmp_path):
    with pytest.raises(cutover_stage.StageUnreadableError):
        cutover_stage.read_cutover_stage(_JOB, tmp_path / "typo")


def test_reads_stage_while_migration_store_is_open(tmp_path, migration_module):
    store = _store(migration_module, tmp_path)
    try:
        _save(migration_module, store, "READY")

        stage = cutover_stage.read_cutover_stage(_JOB, tmp_path)
    finally:
        store.close()

    assert stage == ("READY", None)


def test_reads_failed_stage(tmp_path, migration_module):
    store = _store(migration_module, tmp_path)
    try:
        _save(migration_module, store, "FAILED", failed_stage="FENCING")
    finally:
        store.close()

    assert cutover_stage.read_cutover_stage(_JOB, tmp_path) == (
        "FAILED",
        "FENCING",
    )


def test_missing_job_row_is_not_started(tmp_path, migration_module):
    _store(migration_module, tmp_path).close()

    assert cutover_stage.read_cutover_stage(_JOB, tmp_path) == (
        "not_started",
        None,
    )


def test_read_does_not_write_the_store(tmp_path, migration_module):
    store = _store(migration_module, tmp_path)
    try:
        _save(migration_module, store, "COPYING")
    finally:
        store.close()
    before = (tmp_path / "cutover.sqlite3").read_bytes()

    cutover_stage.read_cutover_stage(_JOB, tmp_path)

    assert (tmp_path / "cutover.sqlite3").read_bytes() == before


@pytest.mark.parametrize(
    ("stage", "checkpoint"),
    [
        ("PAUSING", {}),
        ("FAILED", {}),
        ("FAILED", {"failed_stage": "SOMEWHERE"}),
    ],
)
def test_unknown_or_incomplete_stage_is_unreadable(
    tmp_path, migration_module, stage, checkpoint
):
    store = _store(migration_module, tmp_path)
    try:
        _save(migration_module, store, stage, **checkpoint)
    finally:
        store.close()

    with pytest.raises(cutover_stage.StageUnreadableError):
        cutover_stage.read_cutover_stage(_JOB, tmp_path)


def test_foreign_schema_is_unreadable(tmp_path):
    connection = sqlite3.connect(tmp_path / "cutover.sqlite3")
    connection.execute("CREATE TABLE other (x)")
    connection.commit()
    connection.close()

    with pytest.raises(cutover_stage.StageUnreadableError):
        cutover_stage.read_cutover_stage(_JOB, tmp_path)


def test_cloud_lock_refuses_while_migration_cloud_holds_it(tmp_path):
    with (tmp_path / "cloud.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(cutover_stage.CloudLockBusyError):
            with cutover_stage.cloud_lock(tmp_path):
                pass


def test_cloud_lock_blocks_migration_cloud_while_held(tmp_path):
    with cutover_stage.cloud_lock(tmp_path):
        with (tmp_path / "cloud.lock").open("a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_cloud_lock_refuses_missing_state_dir(tmp_path):
    with pytest.raises(cutover_stage.StageUnreadableError):
        with cutover_stage.cloud_lock(tmp_path / "typo"):
            pass


@pytest.mark.parametrize(
    ("stage", "failed_stage", "expected"),
    [
        ("not_started", None, True),
        ("PRECHECK", None, True),
        ("COPYING", None, True),
        ("READY", None, True),
        ("FAILED", "PRECHECK", True),
        ("FAILED", "COPYING", True),
        ("FAILED", "READY", True),
        ("FENCING", None, False),
        ("SWITCHING", None, False),
        ("COMPLETED", None, False),
        ("NEEDS_RECOVERY", None, False),
        ("FAILED", "FENCING", False),
        ("FAILED", "STOPPING", False),
    ],
)
def test_cutover_not_begun(stage, failed_stage, expected):
    assert cutover_stage.cutover_not_begun(stage, failed_stage) is expected
