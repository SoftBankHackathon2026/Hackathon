"""config.py: YAML 파싱, 환경변수 비밀번호, 비밀번호 비노출."""

from __future__ import annotations

from pathlib import Path

import pytest

from migration.config import ConfigError, load_config, parse_config, redact


def test_connection_spec_hides_password(config, env):
    spec = config.connection("onprem", "app")
    assert spec.user == "migration_app"
    assert spec.password == "app-secret"
    assert "app-secret" not in repr(spec)
    assert "app-secret" not in str(spec)
    assert "app-secret" not in spec.conninfo()
    assert "sslmode=require" in spec.conninfo()


def test_missing_password_env_is_error(config, monkeypatch):
    monkeypatch.delenv("MIGRATION_APP_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="MIGRATION_APP_PASSWORD"):
        config.connection("onprem", "app")


def test_password_in_yaml_rejected():
    raw = {"targets": {"onprem": {"host": "h", "users": {"app": {"user": "u", "password": "x"}}},
                       "aws": {"host": "a"}}}
    with pytest.raises(ConfigError, match="password_env"):
        parse_config(raw)


def test_job_names(config):
    job = config.job
    assert job.slot_name == "forward_001"
    assert job.publication_name == "forward_001_pub"
    assert job.topic == "forward_001.public.migration_demo"
    assert job.source_connector == "forward_001-source"
    assert job.sink_connector == "forward_001-sink"


def test_load_example_yaml():
    path = Path(__file__).resolve().parent.parent / "config.example.yaml"
    cfg = load_config(path)
    assert set(cfg.targets) == {"onprem", "aws"}
    assert cfg.target("onprem").user_spec("cdc").password_env == "MIGRATION_CDC_PASSWORD"
    assert cfg.target("aws").user_spec("admin").password_env == "MIGRATION_AWS_ADMIN_PASSWORD"


def test_unknown_target_and_same_direction():
    with pytest.raises(ConfigError):
        parse_config({"targets": {"a": {"host": "x"}, "b": {"host": "y"}}, "job": {"source": "a", "target": "zzz"}})
    with pytest.raises(ConfigError):
        parse_config({"targets": {"a": {"host": "x"}, "b": {"host": "y"}}, "job": {"source": "a", "target": "a"}})


def test_redact_masks_env_passwords(env):
    assert redact("password=app-secret host=x") == "password=*** host=x"


def test_target_kind_parsed_and_validated():
    from tests.conftest import RAW_CONFIG
    raw = {**RAW_CONFIG, "targets": {k: dict(v) for k, v in RAW_CONFIG["targets"].items()}}
    raw["targets"]["aws"]["kind"] = "rds"
    cfg = parse_config(raw)
    assert cfg.target("aws").kind == "rds" and cfg.target("onprem").kind is None
    raw["targets"]["aws"]["kind"] = "cloud"
    with pytest.raises(ConfigError, match="kind"):
        parse_config(raw)


def test_example_yamls_mark_aws_as_rds():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ("config.example.yaml", "config.return.example.yaml"):
        assert load_config(root / name).target("aws").kind == "rds"


def test_bool_setting_parsed_not_stringified():
    from tests.conftest import RAW_CONFIG
    raw = {**RAW_CONFIG, "cutover": {**RAW_CONFIG["cutover"], "drop_source_slot_on_complete": "false"}}
    assert parse_config(raw).cutover.drop_source_slot_on_complete is False
    raw["cutover"]["drop_source_slot_on_complete"] = "maybe"
    with pytest.raises(ConfigError, match="true/false"):
        parse_config(raw)
