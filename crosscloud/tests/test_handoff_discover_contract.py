"""R1 수집기(discover)의 실제 출력으로 인계 설정을 만드는 계약 시험.

수집기는 가짜 API(test_aws_reader·test_gcp_reader 의 준비물)로 돈다. DB
접속 정보는 Terraform 출력에서, 앱 DB 사용자는 운영자 인자에서 받는다.
"""

import datetime

import test_aws_reader
import test_gcp_reader
import yaml

from crosscloud import handoff

_NOW = datetime.datetime(2026, 11, 7, 15, 30, tzinfo=datetime.UTC)
_TABLES = (handoff.AppTable("public.requests", ("id",)),)


def _tf(value):
    return {"value": value, "type": "string", "sensitive": False}


_AWS_DB = handoff.db_from_terraform(
    "aws",
    {
        "db": _tf(
            {
                # 가짜 RDS(arn ...:db:app)와 같은 식별자.
                "identifier": "app",
                "address": "app.abc.ap-northeast-2.rds.amazonaws.com",
                "port": 5432,
                "name": "shop",
            }
        )
    },
)
_GCP_DB = handoff.db_from_terraform(
    "gcp",
    {
        # 가짜 Cloud SQL(projects/.../instances/app-db)과 같은 이름.
        "db_instance_name": _tf("app-db"),
        "db_public_ip": _tf("34.64.1.2"),
        "database_name": _tf("shop"),
    },
    database="shop_b",
)
_READY = tuple(
    handoff.ReadinessItem(name, True, "ok") for name in handoff.READINESS_NAMES
)


def _aws_inventory():
    clients = test_aws_reader._Clients()
    test_aws_reader._stub_ecs(clients)
    test_aws_reader._stub_image_digest(clients)
    test_aws_reader._stub_resources(clients)
    return test_aws_reader._read(clients)


def _render(inventory, source, target, tmp_path):
    job = handoff.build_job_config(
        inventory,
        source=source,
        target=target,
        source_app_user="shop_app",
        tables=_TABLES,
        control_url="http://127.0.0.1:18080",
        connect_url="http://127.0.0.1:8083",
        now=_NOW,
    )
    path, _ = handoff.render_cloud_config(job, _READY, tmp_path)
    return yaml.safe_load(path.read_text())


def test_renders_from_real_aws_discover_output(tmp_path):
    document = _render(_aws_inventory(), _AWS_DB, _GCP_DB, tmp_path)

    assert document["job"]["source"] == "aws"
    assert document["targets"]["aws"]["host"] == _AWS_DB.endpoint.host
    assert document["targets"]["gcp"]["dbname"] == "shop_b"
    assert document["targets"]["aws"]["users"]["app"]["user"] == "shop_app"


def test_renders_from_real_gcp_discover_output(tmp_path):
    api = test_gcp_reader._FakeApi(test_gcp_reader._pages())
    inventory = test_gcp_reader._read(api)
    aws_target = handoff.TerraformDb(
        "app-new",
        handoff.DbEndpoint("aws", "new.rds.example", 5432, "shop_c"),
    )
    gcp_source = handoff.TerraformDb(
        _GCP_DB.instance_id,
        handoff.DbEndpoint("gcp", "34.64.1.2", 5432, "shop_b"),
    )

    document = _render(inventory, gcp_source, aws_target, tmp_path)

    assert (document["job"]["source"], document["job"]["target"]) == (
        "gcp",
        "aws",
    )
    assert document["targets"]["gcp"]["host"] == "34.64.1.2"
