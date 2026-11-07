import fake_ingress
import pytest

from crosscloud import ingress_control


def test_status_parses_control_state():
    fake = fake_ingress.FakeIngress()
    with fake.serve() as url:
        state = ingress_control.IngressControl(url, token=None).status()

    assert state == ingress_control.IngressState(
        mode="paused", target="onprem", epoch="0", in_flight=0
    )


def test_bearer_token_is_sent_when_configured():
    fake = fake_ingress.FakeIngress(token="t" * 40)
    with fake.serve() as url:
        state = ingress_control.IngressControl(url, token="t" * 40).status()

    assert state.mode == "paused"


def test_missing_token_is_an_error_without_leaking_it():
    fake = fake_ingress.FakeIngress(token="s3cret" * 8)
    with fake.serve() as url:
        with pytest.raises(ingress_control.IngressError) as caught:
            ingress_control.IngressControl(url, token="wrong").status()

    assert caught.value.status == 401
    assert "CONTROL_UNAUTHORIZED" in str(caught.value)
    assert "s3cret" not in str(caught.value)
    assert "wrong" not in str(caught.value)


def test_pause_requires_settled_true():
    fake = fake_ingress.FakeIngress()
    fake.state["mode"] = "running"
    with fake.serve() as url:
        state = ingress_control.IngressControl(url, token=None).pause()

    assert state.mode == "paused"
    assert ("POST", "/pause", {}) in fake.calls


def test_set_target_sends_expected_epoch_and_returns_new_epoch():
    fake = fake_ingress.FakeIngress()
    with fake.serve() as url:
        state = ingress_control.IngressControl(url, token=None).set_target(
            "aws", "0"
        )

    assert (state.target, state.epoch) == ("aws", "1")
    assert (
        "POST",
        "/target",
        {"target": "aws", "expected_epoch": "0"},
    ) in fake.calls


def test_conflict_is_an_error_with_code():
    fake = fake_ingress.FakeIngress()
    with fake.serve() as url:
        with pytest.raises(ingress_control.IngressError) as caught:
            ingress_control.IngressControl(url, token=None).resume("7")

    assert caught.value.status == 409
    assert "EPOCH_CONFLICT" in str(caught.value)


def test_resume_returns_running_state():
    fake = fake_ingress.FakeIngress()
    with fake.serve() as url:
        state = ingress_control.IngressControl(url, token=None).resume("0")

    assert state.mode == "running"


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "paused", "target": "aws", "epoch": "1"},
        {"mode": "stopped", "target": "aws", "epoch": "1", "in_flight": 0},
        {"mode": "paused", "target": "", "epoch": "1", "in_flight": 0},
        {"mode": "paused", "target": "aws", "epoch": "x", "in_flight": 0},
        {"mode": "paused", "target": "aws", "epoch": "1", "in_flight": True},
    ],
)
def test_ambiguous_status_is_an_error(body):
    fake = fake_ingress.FakeIngress()
    fake.status_override = (200, body)
    with fake.serve() as url:
        with pytest.raises(ingress_control.IngressError, match="모호"):
            ingress_control.IngressControl(url, token=None).status()


def test_pause_without_settled_is_an_error(monkeypatch):
    fake = fake_ingress.FakeIngress()
    original = fake.handle

    def unsettled(method, path, body, authorization):
        code, payload = original(method, path, body, authorization)
        if path == "/pause":
            payload = {**payload, "settled": False}
        return code, payload

    monkeypatch.setattr(fake, "handle", unsettled)
    with fake.serve() as url:
        with pytest.raises(ingress_control.IngressError, match="settled"):
            ingress_control.IngressControl(url, token=None).pause()


def test_unreachable_ingress_is_an_error():
    with pytest.raises(ingress_control.IngressError, match="연결"):
        ingress_control.IngressControl(
            "http://127.0.0.1:9", token=None, timeout=1
        ).status()
