from datetime import UTC, datetime, timedelta

from controlplane.domain.fleet import CutoverRecord, MigrationState
from controlplane.domain.migration import Stage
from controlplane.integrations.defaults import DEFAULT_FLEET
from controlplane.services.migrations import stage_views

START = datetime(2026, 11, 8, 1, 45, tzinfo=UTC)


def state(stage, times):
    return MigrationState(
        config=DEFAULT_FLEET.migrations[0],
        cutover=CutoverRecord(stage=stage, checkpoint={}, updated_at=START, stage_times=times),
        gates=[],
        verify_pre=None,
        verify_post=None,
        connectors=[],
        worker=None,
    )


def ends(stage, times):
    return {view.name: view.finished_at for view in stage_views(state(stage, times))}


def test_completed_stage_ends_when_it_starts():
    done = START + timedelta(minutes=12)
    times = [(Stage.COPYING, START), (Stage.VERIFYING, START + timedelta(minutes=10))]
    finished = ends(Stage.COMPLETED, [*times, (Stage.COMPLETED, done)])
    assert finished[Stage.VERIFYING] == done
    assert finished[Stage.COMPLETED] == done
    assert ends(Stage.VERIFYING, times)[Stage.VERIFYING] is None
