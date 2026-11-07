from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import ReadTimeoutError

from controlplane.core.errors import (
    AccessDenied,
    InvalidRequest,
    OperationNotFound,
    ProviderReadFailed,
)
from controlplane.integrations.defaults import ONPREM
from controlplane.main import create_app
from controlplane.schemas.migrations import (
    ComponentView,
    ContainerView,
    EnvironmentView,
    PodView,
)
from controlplane.services.log_redaction import MASK, redact, redact_lines
from controlplane.services.pod_logs import PodLogService, parse_line, simulated_log

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def pod(name="kafka-broker-0", containers=("kafka",), restarts=0):
    return PodView(
        name=name,
        phase="Running",
        ready=True,
        restarts=restarts,
        containers=[ContainerView(name=c, ready=True, restarts=0, image="x") for c in containers],
    )


def component(id="kafka", layer="data", logs=True, namespace="hackathon", pods=None):
    return ComponentView(
        id=id,
        name=id,
        layer=layer,
        kind="statefulset",
        managed=False,
        status="healthy",
        detail="준비 1/1",
        namespace=namespace,
        logs=logs,
        pods=[pod()] if pods is None else pods,
    )


class Fleet:
    def __init__(self, *components):
        self.view = EnvironmentView(
            id="onprem",
            name="온프렘",
            kind="k3s",
            provider="onprem",
            region="seoul",
            role="legacy",
            connected=True,
            observed_at=None,
            components=list(components),
        )

    def environment(self, env_id):
        return (ONPREM, self.view) if env_id == "onprem" else None


class Reader:
    def __init__(self, text="", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def __call__(self, env, namespace, pod, container, *, tail, previous):
        self.calls.append((env.id, namespace, pod, container, tail, previous))
        if self.error:
            raise self.error
        return self.text


def service(reader, *components):
    return PodLogService(Fleet(*components or (component(),)), reader, clock=lambda: NOW)


def test_logs_are_read_from_the_pod_namespace_and_parsed():
    reader = Reader(
        "2026-11-07T11:59:58.123456789Z started broker\n"
        "2026-11-07T11:59:59Z connected to postgres://app:s3cret@db:5432/app\n"
        "no timestamp line\n\n"
    )
    logs = service(reader).logs("onprem", "kafka", "kafka-broker-0", tail=500)
    assert reader.calls == [("onprem", "hackathon", "kafka-broker-0", "kafka", 500, False)]
    assert logs.container == "kafka"
    assert logs.containers == ["kafka"]
    assert [line.message for line in logs.lines] == [
        "started broker",
        f"connected to postgres://app:{MASK}@db:5432/app",
        "no timestamp line",
    ]
    assert logs.lines[0].at == datetime(2026, 11, 7, 11, 59, 58, 123456, tzinfo=UTC)
    assert logs.lines[2].at is None
    assert logs.redacted == 1
    assert logs.truncated is False
    assert logs.fetched_at == NOW
    assert logs.tail == 500


def test_container_and_previous_are_passed_through():
    reader = Reader("")
    target = component(pods=[pod(containers=("app", "sidecar"), restarts=2)])
    logs = service(reader, target).logs(
        "onprem", "kafka", "kafka-broker-0", container="sidecar", previous=True
    )
    assert reader.calls[0][3:] == ("sidecar", 200, True)
    assert logs.containers == ["app", "sidecar"]
    assert logs.previous is True
    assert logs.lines == []


def test_default_container_is_the_first_one():
    reader = Reader("")
    target = component(pods=[pod(containers=("app", "sidecar"))])
    service(reader, target).logs("onprem", "kafka", "kafka-broker-0")
    assert reader.calls[0][3] == "app"


def test_large_logs_are_marked_truncated():
    from controlplane.services.pod_logs import LOG_BYTES

    logs = service(Reader("x" * LOG_BYTES)).logs("onprem", "kafka", "kafka-broker-0")
    assert logs.truncated is True
    almost = service(Reader("x" * (LOG_BYTES - 1))).logs("onprem", "kafka", "kafka-broker-0")
    assert almost.truncated is False


@pytest.mark.parametrize(
    ("env_id", "component_id", "pod_name", "container"),
    [
        ("nowhere", "kafka", "kafka-broker-0", None),
        ("onprem", "nothing", "kafka-broker-0", None),
        ("onprem", "kafka", "gone-0", None),
        ("onprem", "kafka", "kafka-broker-0", "other"),
    ],
)
def test_unknown_targets_are_not_found(env_id, component_id, pod_name, container):
    reader = Reader("")
    with pytest.raises(OperationNotFound):
        service(reader).logs(env_id, component_id, pod_name, container=container)
    assert reader.calls == []


@pytest.mark.parametrize(
    "target",
    [
        component(layer="platform"),
        component(layer="entry"),
        component(logs=False),
        component(namespace=None),
    ],
)
def test_platform_components_are_not_opened(target):
    reader = Reader("")
    with pytest.raises(AccessDenied):
        service(reader, target).logs("onprem", "kafka", "kafka-broker-0")
    assert reader.calls == []


@pytest.mark.parametrize("layer", ["app", "data", "migration"])
def test_workload_layers_are_opened(layer):
    reader = Reader("")
    service(reader, component(layer=layer)).logs("onprem", "kafka", "kafka-broker-0")
    assert len(reader.calls) == 1


@pytest.mark.parametrize("tail", [0, 199, 201, 5000])
def test_tail_must_be_a_listed_choice(tail):
    with pytest.raises(InvalidRequest):
        service(Reader("")).logs("onprem", "kafka", "kafka-broker-0", tail=tail)


@pytest.mark.parametrize(
    ("error", "previous", "kind", "text"),
    [
        (ApiException(status=400), True, OperationNotFound, "재시작 전"),
        (ApiException(status=400), False, ProviderReadFailed, "읽지 못했어요"),
        (ApiException(status=404), False, OperationNotFound, "더 이상 없어요"),
        (ApiException(status=403), False, ProviderReadFailed, "pods/log"),
        (ApiException(status=500), False, ProviderReadFailed, "읽지 못했어요"),
        (ReadTimeoutError(None, "/log", "timed out"), False, ProviderReadFailed, "연결하지"),
        (OSError("down"), False, ProviderReadFailed, "연결하지"),
    ],
)
def test_cluster_errors_become_readable_messages(error, previous, kind, text):
    with pytest.raises(kind) as raised:
        service(Reader(error=error)).logs("onprem", "kafka", "kafka-broker-0", previous=previous)
    assert text in raised.value.message


def test_parse_line_keeps_lines_without_timestamps():
    assert parse_line("2026-11-07T00:00:00Z hello world").message == "hello world"
    assert parse_line("plain text").at is None
    assert parse_line("plain text").message == "plain text"


def test_simulated_log_honours_tail():
    text = simulated_log(ONPREM, "hackathon", "p", "app", tail=200, previous=False)
    assert len(text.splitlines()) == 60
    assert len(simulated_log(ONPREM, "ns", "p", "app", tail=10, previous=False).splitlines()) == 10


@pytest.mark.parametrize(
    ("line", "expected", "count"),
    [
        ("password=hunter2 user=bob", f"password={MASK} user=bob", 1),
        ('{"token": "abc.def", "ok": true}', f'{{"token": "{MASK}", "ok": true}}', 1),
        ("DB_PASSWORD: s3cr3t", f"DB_PASSWORD: {MASK}", 1),
        ("api_key=xyz&page=2", f"api_key={MASK}&page=2", 1),
        ("Set-Cookie: sid=abc; cookie=s3ss10n", f"Set-Cookie: {MASK}; cookie={MASK}", 2),
        ("session_id=deadbeef ok", f"session_id={MASK} ok", 1),
        ("CLIENT_SECRET=zzz", f"CLIENT_SECRET={MASK}", 1),
        ("Authorization: Bearer abcdefghijkl", f"Authorization: {MASK}", 1),
        ("sent bearer abcdefghijkl123 upstream", f"sent bearer {MASK} upstream", 1),
        ("redis://default:pa55@redis:6379/0", f"redis://default:{MASK}@redis:6379/0", 1),
        ("jwt eyJhbGciOiJI.eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4", f"jwt {MASK}", 1),
        ("key AKIAABCDEFGHIJKLMNOP used", f"key {MASK} used", 1),
        ("gh ghp_abcdefghijklmnopqrstuvwxyz0123", f"gh {MASK}", 1),
        ("slack xoxb-1234567890-abcdef", f"slack {MASK}", 1),
        ("openai sk-abcdefghijklmnopqrstuvwx", f"openai {MASK}", 1),
        ("vault hvs.abcdefghijklmnopqrstuvwxyz12", f"vault {MASK}", 1),
        ("GET /healthz 200 2ms", "GET /healthz 200 2ms", 0),
        ("token expired, retrying", "token expired, retrying", 0),
    ],
)
def test_redact_masks_common_secrets(line, expected, count):
    assert redact(line) == (expected, count)


def test_private_key_blocks_are_masked_across_lines():
    lines, count = redact_lines(
        [
            "loading key -----BEGIN RSA PRIVATE KEY-----",
            "MIIEowIBAAKCAQEA",
            "-----END RSA PRIVATE KEY-----",
            "ready",
        ]
    )
    assert lines == [f"loading key {MASK}", MASK, MASK, "ready"]
    assert count == 3


def test_api_refuses_configured_components_without_pods(monkeypatch):
    for name in ("ONEPORT_CONFIG", "ONEPORT_RUNTIME_CONFIG", "ONEPORT_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app()) as client:
        missing = client.get("/api/v1/fleet/environments/nowhere/components/kafka/pods/p/logs")
        closed = client.get("/api/v1/fleet/environments/onprem/components/kafka/pods/p/logs")
    assert missing.status_code == 404
    assert closed.status_code == 403


@pytest.mark.parametrize("line", ["x" * 200_000, "a/b:" * 50_000, "password" * 25_000])
def test_redact_stays_fast_on_long_lines(line):
    import time

    started = time.perf_counter()
    redact(line)
    assert time.perf_counter() - started < 1.0
