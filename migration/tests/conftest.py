"""공통 픽스처: 설정 객체, 임시 상태 디렉터리, 통합 테스트용 DSN."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from migration.config import Config, parse_config

TEST_ENV = {
    "MIGRATION_ADMIN_PASSWORD": "admin-secret",
    "MIGRATION_APP_PASSWORD": "app-secret",
    "MIGRATION_CDC_PASSWORD": "cdc-secret",
    "MIGRATION_SINK_PASSWORD": "sink-secret",
    "MIGRATION_VERIFY_PASSWORD": "verify-secret",
}

RAW_CONFIG = {
    "targets": {
        "onprem": {"host": "onprem-db", "port": 5432, "dbname": "migration_demo", "sslmode": "require"},
        "aws": {"host": "aws-db", "port": 5432, "dbname": "migration_demo", "sslmode": "require"},
    },
    "job": {"job_id": "forward_001", "source": "onprem", "target": "aws"},
    "kafka": {"connect_url": "http://connect:8083"},
    "bot": {"control_url": "http://bot:9091", "request_endpoint": "http://ingress/api/requests"},
    "ingress": {"control_url": "http://ingress:9092", "targets": {"onprem": "onprem", "aws": "aws_db"},
                "app_checks": {"onprem": {"url": "http://app", "environment": "onprem", "database": "migration_demo"},
                               "aws": {"url": "http://app-aws", "environment": "aws", "database": "migration_demo"}}},
    "cutover": {"poll_interval_seconds": 0, "stage_timeout_seconds": 5, "offset_stable_seconds": 0,
                "min_live_rows_after_snapshot": 3, "min_rows_after_switch": 2},
}


@pytest.fixture
def config() -> Config:
    return parse_config(RAW_CONFIG)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    return dict(TEST_ENV)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    return d


def integration_dsn() -> str | None:
    return os.environ.get("MIGRATION_TEST_DSN")


requires_pg = pytest.mark.skipif(integration_dsn() is None, reason="MIGRATION_TEST_DSN 이 없어 통합 테스트 생략")
