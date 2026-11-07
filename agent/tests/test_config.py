from __future__ import annotations

import pytest

from agent.config import ConfigError, parse_config
from tests.conftest import raw_config


def test_parse_minimal(tmp_path):
    cfg = parse_config(raw_config(tmp_path), base_dir=tmp_path)
    assert cfg.store.kind == "file"
    assert cfg.scope.allowed_keys == ("DB_HOST", "DB_PORT")
    assert cfg.plan["digest"].startswith("sha256:")
    assert cfg.plan["remediation"]["auto_apply"] is True


def test_plan_digest_is_stable(tmp_path):
    a = parse_config(raw_config(tmp_path), base_dir=tmp_path).plan["digest"]
    b = parse_config(raw_config(tmp_path), base_dir=tmp_path).plan["digest"]
    assert a == b


@pytest.mark.parametrize("field", ["job_id", "store", "plan", "opa"])
def test_missing_required(tmp_path, field):
    raw = raw_config(tmp_path)
    raw.pop(field)
    with pytest.raises(ConfigError):
        parse_config(raw, base_dir=tmp_path)


def test_target_db_host_required(tmp_path):
    raw = raw_config(tmp_path)
    raw["plan"]["target_db"] = {}
    with pytest.raises(ConfigError, match="target_db"):
        parse_config(raw, base_dir=tmp_path)


def test_unknown_store_kind(tmp_path):
    raw = raw_config(tmp_path)
    raw["store"] = {"kind": "etcd"}
    with pytest.raises(ConfigError, match="store.kind"):
        parse_config(raw, base_dir=tmp_path)


def test_vault_and_aws_parse(tmp_path):
    raw = raw_config(tmp_path, store={"kind": "vault", "vault": {"addr": "http://v:8200", "path": "hackathon/app"}})
    cfg = parse_config(raw, base_dir=tmp_path)
    assert cfg.store.addr == "http://v:8200" and cfg.store.mount == "secret" and cfg.store.secret_path == "hackathon/app"
    raw = raw_config(tmp_path, store={"kind": "aws-secrets-manager", "aws_secrets_manager": {"secret_id": "a/b", "region": "ap-northeast-2"}})
    cfg = parse_config(raw, base_dir=tmp_path)
    assert cfg.store.secret_id == "a/b" and cfg.store.region == "ap-northeast-2"


def test_extra_checks_missing_file_is_empty(tmp_path):
    cfg = parse_config(raw_config(tmp_path, evidence_extra="nope.json"), base_dir=tmp_path)
    assert cfg.extra_checks() == {}
