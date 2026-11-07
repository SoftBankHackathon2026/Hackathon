import dataclasses
import datetime
import json

import pytest
import yaml

from crosscloud import control_adapter, handoff, model

_NOW = datetime.datetime(2026, 11, 7, 15, 30, 0, tzinfo=datetime.UTC)
_TABLES = (handoff.AppTable("public.requests", ("id",)),)


def _inventory(cloud="aws", native_id="arn:aws:rds:ap-northeast-2:1:db:app"):
    resource = model.Resource(
        kind="postgres_instance",
        cloud=cloud,
        native_id=native_id,
        region="ap-northeast-2",
        attributes=(),
        source_of_truth="api",
    )
    return model.Inventory(
        cloud=cloud,
        collected_at=_NOW,
        resources=(resource,),
        workloads=(),
        grants=(),
        restrictions=(),
        restriction_scans=(),
        couplings=(),
        omissions=(),
        attestation=model.CollectionAttestation(
            secret_value_calls=0,
            collector_can_read_secret_values=False,
            cai_viewer_bound=False,
            suspected_values_retained=0,
            evidence_ref="none",
        ),
    )


def _tf(value):
    return {"value": value, "type": "string", "sensitive": False}


# terraform output -json 의 모양(terraform/aws·google/target_base outputs.tf).
_AWS_OUTPUTS = {
    "db": _tf(
        {
            "identifier": "app",
            "address": "app.abc.ap-northeast-2.rds.amazonaws.com",
            "port": 5432,
            "name": "shop",
            "username": "postgres",
        }
    ),
    "region": _tf("ap-northeast-2"),
}
_GCP_OUTPUTS = {
    "db_instance_name": _tf("app-db"),
    "db_public_ip": _tf("34.64.1.2"),
    "database_name": _tf("shop"),
}
_SOURCE = handoff.db_from_terraform("aws", _AWS_OUTPUTS)
_TARGET_DB = handoff.db_from_terraform("gcp", _GCP_OUTPUTS, database="shop_b")
_TARGET = _TARGET_DB.endpoint


def _job(**overrides):
    arguments = {
        "source": _SOURCE,
        "target": _TARGET_DB,
        "source_app_user": "shop_app",
        "tables": _TABLES,
        "control_url": "http://127.0.0.1:18080",
        "connect_url": "http://127.0.0.1:8083",
        "now": _NOW,
    }
    arguments.update(overrides)
    inventory = arguments.pop("inventory", _inventory())
    return handoff.build_job_config(inventory, **arguments)


def _all_ready():
    return tuple(
        handoff.ReadinessItem(name=name, ready=True, detail="ok")
        for name in handoff.READINESS_NAMES
    )


def test_new_job_id_differs_per_direction_and_fits_migration_rule():
    forward = handoff.new_job_id("aws", "gcp", _NOW)
    back = handoff.new_job_id("gcp", "aws", _NOW)

    assert forward != back
    assert forward == "ccx_aws_gcp_20261107153000"
    assert len(forward) <= 40


def test_db_from_terraform_reads_rds_outputs():
    assert _SOURCE == handoff.TerraformDb(
        instance_id="app",
        endpoint=handoff.DbEndpoint(
            "aws", "app.abc.ap-northeast-2.rds.amazonaws.com", 5432, "shop"
        ),
    )


def test_db_from_terraform_reads_cloud_sql_outputs_with_job_database():
    assert _TARGET_DB == handoff.TerraformDb(
        instance_id="app-db",
        endpoint=handoff.DbEndpoint("gcp", "34.64.1.2", 5432, "shop_b"),
    )


@pytest.mark.parametrize(
    ("cloud", "outputs", "missing"),
    [
        ("aws", {}, "db"),
        ("aws", {"db": _tf({"identifier": "app", "port": 5432})}, "address"),
        ("gcp", {"db_instance_name": _tf("app-db")}, "db_public_ip"),
    ],
)
def test_db_from_terraform_names_missing_output(cloud, outputs, missing):
    with pytest.raises(ValueError, match=missing):
        handoff.db_from_terraform(cloud, outputs)


def test_build_job_config_uses_terraform_db_and_operator_app_user():
    job = _job()

    assert job.source == _SOURCE.endpoint
    assert job.target == _TARGET
    assert job.job_id == "ccx_aws_gcp_20261107153000"
    app_users = {
        login.user
        for login in job.source_logins + job.target_logins
        if login.role == "app"
    }
    assert app_users == {"shop_app"}


def test_build_job_config_refuses_db_not_in_inventory():
    inventory = _inventory(native_id="arn:aws:rds:ap-northeast-2:1:db:other")

    with pytest.raises(ValueError, match="app"):
        _job(inventory=inventory)


def test_build_job_config_refuses_inventory_of_other_cloud():
    inventory = _inventory(cloud="gcp", native_id="projects/p/instances/app")

    with pytest.raises(ValueError, match="cloud"):
        _job(inventory=inventory)


def test_job_rejects_same_database_name_on_both_sides():
    # 이름이 같으면 대상 앱이 아직 원본에 붙어 있어도 /api/info 로 못 가른다.
    same = handoff.db_from_terraform("gcp", _GCP_OUTPUTS)

    with pytest.raises(ValueError, match="dbname"):
        _job(target=same)


def test_job_rejects_same_cloud_on_both_sides():
    with pytest.raises(ValueError, match="cloud"):
        dataclasses.replace(
            _job(), target=dataclasses.replace(_TARGET, cloud="aws")
        )


def test_job_rejects_control_url_off_loopback():
    with pytest.raises(ValueError, match="127.0.0.1"):
        dataclasses.replace(_job(), control_url="http://10.0.0.5:18080")


def test_job_rejects_app_login_reusing_migration_role_name():
    with pytest.raises(ValueError, match="app"):
        _job(source_app_user="migration_sink")


@pytest.mark.parametrize(
    "table",
    [
        handoff.AppTable("requests", ("id",)),
        handoff.AppTable("public.Requests", ("id",)),
        handoff.AppTable("public.requests", ()),
        handoff.AppTable("public.requests", ("id", "id")),
    ],
)
def test_job_rejects_table_the_migration_tool_would_reject(table):
    with pytest.raises(ValueError, match="table"):
        dataclasses.replace(_job(), tables=(table,))


def test_render_writes_labels_tables_urls_and_only_env_names(tmp_path):
    path, env_names = handoff.render_cloud_config(
        _job(), _all_ready(), tmp_path
    )

    document = yaml.safe_load(path.read_text())
    assert path.name == "config.cloud.yaml"
    assert set(document["targets"]) == {"aws", "gcp"}
    assert document["job"] == {
        "job_id": "ccx_aws_gcp_20261107153000",
        "source": "aws",
        "target": "gcp",
    }
    assert document["targets"]["gcp"]["sslmode"] == "require"
    assert document["targets"]["aws"]["users"]["cdc"] == {
        "user": "migration_cdc",
        "password_env": "AWS_CDC_PASSWORD",
    }
    assert "sink" in document["targets"]["gcp"]["users"]
    assert document["application"] == {
        "control_url": "http://127.0.0.1:18080",
        "tables": [{"name": "public.requests", "primary_key": ["id"]}],
    }
    assert document["kafka"] == {"connect_url": "http://127.0.0.1:8083"}
    assert "password:" not in path.read_text()
    assert env_names == (
        "AWS_ADMIN_PASSWORD",
        "AWS_CDC_PASSWORD",
        "AWS_VERIFY_PASSWORD",
        "GCP_ADMIN_PASSWORD",
        "GCP_SINK_PASSWORD",
        "GCP_VERIFY_PASSWORD",
    )


def test_render_refuses_when_any_readiness_item_is_not_ready(tmp_path):
    items = _all_ready()
    blocked = items[:1] + (
        dataclasses.replace(items[1], ready=False, detail="no"),
    )
    blocked += items[2:]

    with pytest.raises(handoff.NotReadyError, match=items[1].name):
        handoff.render_cloud_config(_job(), blocked, tmp_path)
    assert not (tmp_path / "config.cloud.yaml").exists()


def test_render_refuses_when_a_readiness_item_is_missing(tmp_path):
    with pytest.raises(
        handoff.NotReadyError, match=handoff.READINESS_NAMES[-1]
    ):
        handoff.render_cloud_config(_job(), _all_ready()[:-1], tmp_path)


def test_rendered_config_is_accepted_by_migration_cloud_parser(
    tmp_path, migration_module
):
    plan_module = migration_module("migration.cloud.plan")
    path, _ = handoff.render_cloud_config(_job(), _all_ready(), tmp_path)

    plan = plan_module.load(path)

    assert plan.config.job.source == "aws"
    assert plan.config.job.target == "gcp"
    assert plan.application_url == "http://127.0.0.1:18080"
    assert [t.name for t in plan.tables] == ["public.requests"]


def test_reverse_job_flips_labels_and_gets_new_job_id(
    tmp_path, migration_module
):
    plan_module = migration_module("migration.cloud.plan")
    back = handoff.CloudJobConfig(
        job_id=handoff.new_job_id("gcp", "aws", _NOW),
        source=_TARGET,
        target=handoff.DbEndpoint("aws", "new-rds.example", 5432, "shop"),
        source_logins=handoff.standard_logins("gcp", "source", "shop_app"),
        target_logins=handoff.standard_logins("aws", "target", "shop_app"),
        sslmode="require",
        tables=_TABLES,
        control_url="http://127.0.0.1:18080",
        connect_url="http://127.0.0.1:8083",
    )

    path, env_names = handoff.render_cloud_config(back, _all_ready(), tmp_path)

    plan = plan_module.load(path)
    assert (plan.config.job.source, plan.config.job.target) == ("gcp", "aws")
    assert "AWS_SINK_PASSWORD" in env_names
    assert "GCP_CDC_PASSWORD" in env_names


_INGRESS_TARGETS = (
    handoff.IngressTarget("aws", "http://ccx-alb.example"),
    handoff.IngressTarget("gcp", "http://34.111.2.3"),
)


def test_ingress_env_lists_both_labels_and_source_alias(tmp_path):
    path, secret_envs = handoff.render_ingress_env(
        _job(), _INGRESS_TARGETS, "redis://redis:6379", tmp_path
    )

    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert path.name == "ingress.env"
    assert json.loads(values["INGRESS_TARGETS"]) == {
        "aws": {"url": "http://ccx-alb.example"},
        "gcp": {"url": "http://34.111.2.3"},
        "onprem": {"url": "http://ccx-alb.example"},
    }
    assert values["REDIS_URL"] == "redis://redis:6379"
    assert values["INGRESS_CONTROL_HOST"] == "0.0.0.0"
    assert "INGRESS_CONTROL_TOKEN" not in values
    assert secret_envs == ("INGRESS_CONTROL_TOKEN",)


def test_ingress_env_includes_target_token_env_names(tmp_path):
    targets = (
        handoff.IngressTarget(
            "aws", "https://a.example", "AWS_REQUEST_API_TOKEN"
        ),
        handoff.IngressTarget("gcp", "http://34.111.2.3"),
    )

    path, secret_envs = handoff.render_ingress_env(
        _job(), targets, "redis://redis:6379", tmp_path
    )

    assert secret_envs == ("AWS_REQUEST_API_TOKEN", "INGRESS_CONTROL_TOKEN")
    assert '"token_env":"AWS_REQUEST_API_TOKEN"' in path.read_text()


def test_ingress_env_readable_by_control_adapter(tmp_path):
    handoff.render_cloud_config(_job(), _all_ready(), tmp_path)
    handoff.render_ingress_env(
        _job(), _INGRESS_TARGETS, "redis://redis:6379", tmp_path
    )

    config = control_adapter.load_adapter_config(
        tmp_path / "config.cloud.yaml", tmp_path / "ingress.env"
    )

    assert (config.source, config.target, config.port) == ("aws", "gcp", 18080)
    assert config.target_app_url == "http://34.111.2.3"


@pytest.mark.parametrize(
    ("targets", "redis_url", "match"),
    [
        (_INGRESS_TARGETS[:1], "redis://r:6379", "gcp"),
        (
            (
                handoff.IngressTarget("aws", "http://a", "AWS_TOKEN"),
                _INGRESS_TARGETS[1],
            ),
            "redis://r:6379",
            "https",
        ),
        (
            (handoff.IngressTarget("aws", "http://u:p@a"), _INGRESS_TARGETS[1]),
            "redis://r:6379",
            "url",
        ),
        (
            (handoff.IngressTarget("aws", "ftp://a"), _INGRESS_TARGETS[1]),
            "redis://r:6379",
            "url",
        ),
        (_INGRESS_TARGETS, "http://r:6379", "REDIS_URL"),
    ],
)
def test_ingress_env_rejects_unsafe_settings(
    tmp_path, targets, redis_url, match
):
    with pytest.raises(ValueError, match=match):
        handoff.render_ingress_env(_job(), targets, redis_url, tmp_path)
    assert not (tmp_path / "ingress.env").exists()


_CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def test_app_tls_env_verifies_db_certificate_with_ca_content():
    env = handoff.app_tls_env(_CA_PEM)

    assert env == (
        model.KeyValue("PGSSLMODE", "verify-full"),
        model.KeyValue("DB_SSL_CA", _CA_PEM),
    )


@pytest.mark.parametrize("pem", ["", "not a certificate", "/etc/ca.pem"])
def test_app_tls_env_rejects_non_pem_ca(pem):
    with pytest.raises(ValueError, match="PEM"):
        handoff.app_tls_env(pem)


def test_render_does_not_wait_for_ingress_start(tmp_path):
    # ingress-start 는 render 뒤에 돈다 — 그 결과를 렌더링 조건으로 두면 순환.
    items = tuple(
        handoff.ReadinessItem(name, True, "ok")
        for name in (
            "replication_flags",
            "migration_logins",
            "target_schema_empty",
            "profile_secrets",
        )
    )

    path, _ = handoff.render_cloud_config(_job(), items, tmp_path)

    assert path.exists()
    assert "ingress_on_source" not in handoff.READINESS_NAMES
