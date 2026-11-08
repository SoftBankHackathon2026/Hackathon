import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_agent_console import BEARER, RUN, TOKEN, put, run_body

from controlplane.api.routes import slack as slack_route
from controlplane.main import create_app
from controlplane.providers.registry import ProviderRegistry
from controlplane.services.slack_approval import (
    SlackApproval,
    decided_message,
    read_decision,
    read_payload,
    safe_response_url,
)

SECRET = "slack-signing-secret-0123456789abcdef"
RESPONSE_URL = "https://hooks.slack.com/actions/T1/123/abc"
SECTION = {"type": "section", "text": {"type": "mrkdwn", "text": "승인 대기"}}
ACTIONS = {"type": "actions", "elements": []}


def payload(action_id="agent_approve", run_id=RUN, user_id="U1", url=RESPONSE_URL, **extra):
    return {
        "type": "block_actions",
        "user": {"id": user_id, "name": "yubin"},
        "actions": [{"action_id": action_id, "value": run_id}],
        "response_url": url,
        "message": {"blocks": [SECTION, ACTIONS]},
        **extra,
    }


def signed(body: bytes, secret: str = SECRET, ts: int | None = None) -> dict:
    stamp = str(int(time.time()) if ts is None else ts)
    digest = hmac.new(secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256)
    return {
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": "v0=" + digest.hexdigest(),
        "Content-Type": "application/x-www-form-urlencoded",
    }


def form(doc: dict) -> bytes:
    return urlencode({"payload": json.dumps(doc)}).encode()


def test_signature_checks_secret_body_and_age():
    slack = SlackApproval(SecretStr(SECRET), frozenset())
    body = form(payload())
    now = time.time()
    headers = signed(body)
    sig, ts = headers["X-Slack-Signature"], headers["X-Slack-Request-Timestamp"]
    assert slack.verify(body, ts, sig, now)
    assert not slack.verify(body + b"x", ts, sig, now)
    assert not slack.verify(body, ts, signed(body, "other-secret")["X-Slack-Signature"], now)
    assert not slack.verify(body, ts, sig, now + 301)
    assert not slack.verify(body, None, sig, now)
    assert not slack.verify(body, "abc", sig, now)


def test_decision_parsing_rejects_foreign_urls_and_unknown_actions():
    got = read_decision(payload())
    assert got is not None and (got.run_id, got.decision, got.user_id) == (RUN, "approve", "U1")
    assert got.requested_by == "yubin, Slack"
    assert read_decision(payload("agent_reject")).decision == "reject"
    assert read_decision(payload("agent_open")) is None
    assert read_decision(payload(run_id="run-../../x")) is None
    assert read_decision(payload(url="https://evil.example/hooks.slack.com")) is None
    assert read_decision(payload(url="http://hooks.slack.com/x")) is None
    assert read_decision({**payload(), "type": "view_submission"}) is None
    assert not safe_response_url("https://hooks.slack.com:8443/x")
    assert read_payload(b"not a form") is None
    weird = read_decision(payload(user_id="U9", **{"user": {"id": "U9", "name": "<!channel>"}}))
    assert weird.requested_by == "channel, Slack"


def test_decided_message_drops_buttons_and_names_the_person():
    message = decided_message(read_decision(payload()))
    assert message["replace_original"] is True
    assert [b["type"] for b in message["blocks"]] == ["section", "context"]
    assert "<@U1> 님이 승인했어요" in message["blocks"][1]["elements"][0]["text"]


@pytest.fixture
def slack_client(monkeypatch, store):
    monkeypatch.delenv("ONEPORT_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("ONEPORT_FLEET_CONFIG", raising=False)
    monkeypatch.setenv("ONEPORT_INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("ONEPORT_SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("ONEPORT_SLACK_APPROVERS", "U1, U2")
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(slack_route, "reply", lambda url, body: sent.append((url, body)))
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(app) as client:
        client.sent = sent
        yield client


def click(client, doc: dict, **header_overrides):
    body = form(doc)
    return client.post(
        "/api/v1/slack/interactions", content=body, headers={**signed(body), **header_overrides}
    )


def test_slack_click_queues_the_same_approval_the_agent_reads(slack_client):
    client = slack_client
    assert put(client, run_body()).status_code == 204
    assert click(client, payload()).status_code == 200
    queued = client.get("/api/v1/ingest/agent/approvals?agent_id=agent-a", headers=BEARER).json()
    assert [(a["run_id"], a["decision"], a["requested_by"]) for a in queued] == [
        (RUN, "approve", "yubin, Slack")
    ]
    url, body = client.sent[-1]
    assert url == RESPONSE_URL and body["replace_original"] is True

    assert click(client, payload("agent_reject")).status_code == 200
    assert client.sent[-1][1]["response_type"] == "ephemeral"
    assert (
        len(client.get("/api/v1/ingest/agent/approvals?agent_id=agent-a", headers=BEARER).json())
        == 1
    )


def test_slack_rejects_bad_signature_strangers_and_unknown_runs(slack_client):
    client = slack_client
    assert put(client, run_body()).status_code == 204
    assert click(client, payload(), **{"X-Slack-Signature": "v0=00"}).status_code == 401
    assert client.sent == []
    assert click(client, payload(user_id="U3")).status_code == 200
    assert "승인할 수 있는 사람이 아니에요" in client.sent[-1][1]["text"]
    assert click(client, payload(run_id="run-ffffffff")).status_code == 200
    assert client.sent[-1][1]["text"] == slack_route.RUN_GONE
    big = form({**payload(), "pad": "x" * 70_000})
    assert (
        client.post("/api/v1/slack/interactions", content=big, headers=signed(big)).status_code
        == 413
    )
    assert click(client, payload("agent_open")).status_code == 200
    assert len(client.sent) == 2
    assert (
        client.get("/api/v1/ingest/agent/approvals?agent_id=agent-a", headers=BEARER).json() == []
    )


def test_slack_endpoint_is_off_without_secret(monkeypatch, store):
    monkeypatch.delenv("ONEPORT_SLACK_SIGNING_SECRET", raising=False)
    app = create_app(registry=ProviderRegistry(), store=store, auth_mode="demo")
    with TestClient(app) as client:
        body = form(payload())
        assert click(client, payload()).status_code == 503
        assert client.post("/api/v1/slack/interactions", content=body).status_code == 503
