"""운영자 PC compose 예시 — 기동 전에 실패할 값이 없는지."""

import base64
import pathlib

import yaml

_DEPLOY = pathlib.Path(__file__).parents[1] / "deploy" / "operator-pc"


def test_kafka_cluster_id_is_base64url_of_16_bytes():
    # KRaft 는 CLUSTER_ID 를 16바이트 UUID 의 base64url(22자)로만 받는다.
    compose = yaml.safe_load((_DEPLOY / "connect.compose.yaml").read_text())
    cluster_id = compose["services"]["kafka"]["environment"]["CLUSTER_ID"]

    assert len(cluster_id) == 22
    assert len(base64.urlsafe_b64decode(cluster_id + "==")) == 16
