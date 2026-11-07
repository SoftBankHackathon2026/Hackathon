"""seed.py: 결정적 ID 와 레코드."""

from __future__ import annotations

import uuid
from dataclasses import replace
import pytest

from migration.config import ConfigError
from migration.seed import run

from migration.seed import SEED_COUNT, seed_records, seed_request_id


def test_seed_ids_are_deterministic_and_unique():
    ids = [seed_request_id(n) for n in range(1, SEED_COUNT + 1)]
    assert len(set(ids)) == SEED_COUNT
    assert seed_request_id(1) == uuid.uuid5(uuid.UUID("5f3c1c2e-7b7a-4e3f-9f4e-2c1a0b9d8e7f"), "seed-1")
    assert [str(i) for i in ids] == [r.request_id for r in seed_records()]


def test_seed_records_fields():
    recs = seed_records(3)
    assert [r.request_no for r in recs] == [1, 2, 3]
    assert all(r.phase == "seed" and r.run_id == "seed" for r in recs)
    assert recs[0].created_at == "2026-01-01T00:00:01.000000+00:00"
    assert seed_records(3) == recs


def test_demo_wrong_seed_count_is_rejected_before_connecting(config):
    config = replace(config, cutover=replace(config.cutover, demo_total_rows=1000,
                     min_live_rows_after_snapshot=100, min_rows_after_switch=100))
    with pytest.raises(ConfigError, match='800 seed'):
        run(config, 'onprem', state_dir=None, count=1000)
