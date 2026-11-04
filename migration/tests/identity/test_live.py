"""Opt-in real read-only collection, never permission mutation or deny writes."""
import os
import pytest
from migration.identity.collect import collect
from migration.identity.config import load_spec
from migration.identity.registry import from_environment


@pytest.mark.identity_live
def test_live_readonly_collection():
    path = os.environ.get('IDENTITY_LIVE_SPEC')
    if not path or os.environ.get('IDENTITY_LIVE_READONLY') != '1':
        pytest.skip('실제 시험 범위와 조회 자격증명 없음: IDENTITY_LIVE_SPEC 및 IDENTITY_LIVE_READONLY=1 필요')
    spec = load_spec(path)
    inventory = collect(spec, from_environment(spec.source), from_environment(spec.target))
    for side in (inventory.source, inventory.target):
        assert not any(status in {'failed','denied'} for status in side['coverage'].values())
    # Passing this test means only selected read APIs worked, never effective
    # permission equivalence or a successful migration.
