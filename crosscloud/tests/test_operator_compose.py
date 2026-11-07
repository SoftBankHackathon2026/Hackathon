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


def _load(name):
    return yaml.safe_load((_DEPLOY / name).read_text())


def test_ingress_env_file_can_point_at_rehearsal_state_dir():
    service = _load("ingress.compose.yaml")["services"]["durable-ingress"]

    assert service["env_file"] == ["${INGRESS_ENV_FILE:-./out/ingress.env}"]


def test_traffic_bot_sends_only_to_ingress_and_starts_on_request():
    bot = _load("ingress.compose.yaml")["services"]["traffic-bot"]

    assert bot["profiles"] == ["bot"]
    assert bot["environment"]["BOT_BASE_URL"] == "http://durable-ingress:8080"
    assert "BOT_API_TOKEN" not in bot["environment"]
    assert "ports" not in bot
    assert bot["labels"] == {"com.ghost.crosscloud.session": "${SESSION}"}


def test_return_override_adds_reverse_direction_passwords():
    override = _load("connect.return.compose.yaml")

    env = override["services"]["connect"]["environment"]
    assert set(env) == {"GCP_CDC_PASSWORD", "AWS_SINK_PASSWORD"}
    assert all(value.startswith("${") for value in env.values())
