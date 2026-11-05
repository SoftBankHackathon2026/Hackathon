"""Login mode: email/password/team accounts, cookie sessions and team-scoped releases."""

import pytest
from fastapi.testclient import TestClient
from test_argo import enqueue
from test_argo import harness as harness
from test_argo import sdk as sdk

from controlplane.core.security import hash_password, verify_password
from controlplane.main import create_app
from controlplane.providers.onprem.adapter import OnpremAdapter
from controlplane.providers.registry import ProviderRegistry
from controlplane.repositories import accounts
from controlplane.repositories.accounts import SESSION_COOKIE

ORIGIN = {"Origin": "http://127.0.0.1:4320"}
PASSWORD = "correct-horse-battery"


OPENED = []


@pytest.fixture(autouse=True)
def close_clients():
    """Leave each app's lifespan so its database pool is closed with the test."""
    yield
    while OPENED:
        OPENED.pop().__exit__(None, None, None)


@pytest.fixture
def app(harness):
    registry = ProviderRegistry()
    registry.register(OnpremAdapter(harness.runtime.settings))
    return create_app(registry=registry, store=harness.store, auth_mode="login")


def member(app, email, team):
    """A browser signed up as `email` in `team` (each client keeps its own cookie)."""
    client = TestClient(app, headers=ORIGIN)
    client.__enter__()
    OPENED.append(client)
    created = client.post(
        "/api/v1/auth/signup", json={"email": email, "password": PASSWORD, "team": team}
    )
    assert created.status_code == 201, created.text
    return client


def release_body(h, key="login-idempotency-key-0001"):
    return {"request": h.request.model_dump(mode="json"), "idempotency_key": key}


def test_password_hash_verifies_only_the_password():
    stored = hash_password(PASSWORD)
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "x", stored)
    assert not verify_password(PASSWORD, "plain-text") and stored != hash_password(PASSWORD)


def test_sign_up_log_in_and_log_out(app, harness):
    with TestClient(app, headers=ORIGIN) as client:
        assert client.get("/api/v1/access").json() == {
            "auth_mode": "login",
            "execution_configured": True,
            "user": None,
        }
        assert client.get("/api/v1/releases").status_code == 401
        assert client.get("/api/v1/environments").status_code == 401
        assert client.post("/api/v1/plans", json={}).status_code == 401
        assert client.get("/api/v1/builds").status_code == 401
        assert client.get("/api/v1/environments/test/traffic").status_code == 401

        body = {"email": " Dev@Example.com ", "password": PASSWORD, "team": "  Platform   Team "}
        created = client.post(
            "/api/v1/auth/signup", json=body, headers={"X-Forwarded-Proto": "https"}
        )
        assert created.status_code == 201
        assert created.json() == {
            "email": "dev@example.com",
            "team": "platform team",
            "role": "owner",
        }
        cookie = created.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
        assert "Path=/api" in cookie

        # Only hashes are stored.
        with harness.store.connect() as conn:
            user = conn.execute("SELECT * FROM platform_users").fetchone()
            session = conn.execute("SELECT * FROM platform_sessions").fetchone()
        assert PASSWORD not in user["password_hash"]
        assert session["token_hash"] not in cookie

        assert client.post("/api/v1/auth/signup", json=body).status_code == 409
        for bad in (
            {**body, "email": "not-an-email"},
            {**body, "email": "new@example.com", "password": "short"},
            {**body, "email": "new@example.com", "team": " "},
        ):
            assert client.post("/api/v1/auth/signup", json=bad).status_code == 422

    with TestClient(app, headers=ORIGIN) as client:
        credentials = {"email": "dev@example.com", "password": PASSWORD}
        assert (
            client.post("/api/v1/auth/login", json=credentials, headers={"Origin": ""}).status_code
            == 403
        )
        wrong = client.post(
            "/api/v1/auth/login", json={**credentials, "password": "wrong-password"}
        )
        assert wrong.status_code == 401 and SESSION_COOKIE not in client.cookies
        unknown = {"email": "nobody@example.com", "password": PASSWORD}
        assert client.post("/api/v1/auth/login", json=unknown).status_code == 401

        assert client.post("/api/v1/auth/login", json=credentials).status_code == 200
        assert client.get("/api/v1/access").json()["user"]["team"] == "platform team"
        assert client.get("/api/v1/releases").json() == []
        assert client.post("/api/v1/auth/logout").status_code == 204
        assert client.get("/api/v1/access").json()["user"] is None
        assert client.get("/api/v1/releases").status_code == 401


def test_repeated_failures_are_throttled_per_email(app):
    member(app, "dev@example.com", "platform")
    with TestClient(app, headers=ORIGIN) as client:
        wrong = {"email": "dev@example.com", "password": "wrong-password"}
        assert [client.post("/api/v1/auth/login", json=wrong).status_code for _ in range(6)] == [
            401,
            401,
            401,
            401,
            401,
            429,
        ]
        other = {"email": "other@example.com", "password": "wrong-password"}
        assert client.post("/api/v1/auth/login", json=other).status_code == 401


def test_releases_and_environments_are_scoped_to_the_team(app, harness):
    h = harness
    legacy = enqueue(h, "legacy-operator-key-0001")
    with h.store.connect() as conn:
        conn.execute("UPDATE releases SET state='succeeded' WHERE id=%s", (legacy["id"],))
    alice = member(app, "alice@example.com", "platform")
    teammate = member(app, "amy@example.com", "Platform")
    bob = member(app, "bob@example.com", "data")

    # A release made before login mode has no team, so no team lists it.
    assert alice.get("/api/v1/releases").json() == []
    assert alice.get("/api/v1/releases/" + legacy["id"]).status_code == 404
    assert alice.get("/api/v1/environments").json()[0]["team"] is None

    created = alice.post("/api/v1/releases", json=release_body(h))
    assert created.status_code == 202
    release = created.json()
    assert (release["team"], release["created_by"]) == ("platform", "alice@example.com")

    # The same team sees the release and the environment it now owns.
    assert [r["id"] for r in teammate.get("/api/v1/releases").json()] == [release["id"]]
    assert teammate.get("/api/v1/releases/" + release["id"]).status_code == 200
    own = teammate.get("/api/v1/environments").json()[0]
    assert own["team"] == "platform" and own["application"] == "demo"

    # Another team sees neither the release nor the app behind the environment.
    assert bob.get("/api/v1/releases").json() == []
    assert bob.get("/api/v1/releases/" + release["id"]).status_code == 404
    action = bob.post(f"/api/v1/releases/{release['id']}/actions", json={"action": "rollback"})
    assert action.status_code == 404
    hidden = bob.get("/api/v1/environments").json()[0]
    assert hidden == {
        "id": "test",
        "name": "다른 팀이 사용 중인 환경",
        "runtime": "onprem_argo",
        "state": "claimed",
        "executable": False,
        "connection_verified": False,
    }
    taken = bob.post("/api/v1/releases", json=release_body(h, "login-idempotency-key-0002"))
    assert taken.status_code == 409 and "다른 팀" in taken.json()["detail"]
    # Replaying the other team's request key does not return its release.
    assert bob.post("/api/v1/releases", json=release_body(h)).status_code == 409
    # Planning validates against the registered app, so it is refused as well.
    assert bob.post("/api/v1/plans", json=release_body(h)["request"]).status_code == 409
    assert alice.post("/api/v1/plans", json=release_body(h)["request"]).status_code == 200
    # A failed request from the owning team does not give the environment up.
    busy = alice.post("/api/v1/releases", json=release_body(h, "login-idempotency-key-0003"))
    assert busy.status_code == 409
    assert bob.get("/api/v1/environments").json()[0]["state"] == "claimed"
    for path in ("logs", "traffic", "traces"):
        assert bob.get(f"/api/v1/environments/test/{path}").status_code == 404
        # The owner reaches the provider, which has no log or trace reader here.
        assert alice.get(f"/api/v1/environments/test/{path}").status_code == 409


def test_a_rejected_first_deploy_does_not_keep_the_environment(app, harness):
    alice = member(app, "alice@example.com", "platform")
    bob = member(app, "bob@example.com", "data")
    invalid = release_body(harness)
    invalid["request"]["source"]["image"] = "docker.io/library/unknown@sha256:" + "0" * 64
    assert alice.post("/api/v1/releases", json=invalid).status_code == 422
    assert bob.get("/api/v1/environments").json()[0]["team"] is None
    assert bob.post("/api/v1/releases", json=release_body(harness)).status_code == 202
    assert alice.get("/api/v1/environments").json()[0]["state"] == "claimed"


def test_other_modes_have_no_accounts(harness):
    registry = ProviderRegistry()
    registry.register(OnpremAdapter(harness.runtime.settings))
    demo = create_app(registry=registry, store=harness.store, auth_mode="demo")
    with TestClient(demo, headers=ORIGIN) as client:
        body = {"email": "dev@example.com", "password": PASSWORD, "team": "platform"}
        assert client.post("/api/v1/auth/signup", json=body).status_code == 404
        assert client.get("/api/v1/access").json()["user"] is None
        created = client.post("/api/v1/releases", json=release_body(harness))
        assert created.status_code == 202 and created.json()["team"] is None
        assert "team" not in client.get("/api/v1/environments").json()[0]


def test_active_sessions_stay_signed_in_without_a_query_per_request(app, harness, monkeypatch):
    alice = member(app, "alice@example.com", "platform")

    def expiry():
        with harness.store.connect() as conn:
            return conn.execute(
                "SELECT expires_at - now() AS left FROM platform_sessions"
            ).fetchone()["left"]

    assert expiry() > accounts.SESSION_TTL - accounts.SESSION_RENEW_AFTER
    # A session close to expiry is pushed out again by use, and the cookie is re-issued.
    with harness.store.connect() as conn:
        conn.execute("UPDATE platform_sessions SET expires_at = now() + interval '2 hours'")
    monkeypatch.setattr(accounts, "SESSION_CACHE_SECONDS", 0)
    loaded = alice.get("/api/v1/access")
    assert loaded.json()["user"]["email"] == "alice@example.com"
    assert f"Max-Age={int(accounts.SESSION_TTL.total_seconds())}" in loaded.headers["set-cookie"]
    assert expiry() > accounts.SESSION_TTL - accounts.SESSION_RENEW_AFTER

    # Polling reuses the verified session instead of reading the database every time.
    monkeypatch.setattr(accounts, "SESSION_CACHE_SECONDS", 30)
    assert alice.get("/api/v1/releases").status_code == 200
    with harness.store.connect() as conn:
        conn.execute("UPDATE platform_sessions SET expires_at = now() - interval '1 second'")
    assert alice.get("/api/v1/releases").status_code == 200
    # Once the cache lapses the expired session is rejected; logging out is immediate.
    monkeypatch.setattr(accounts, "SESSION_CACHE_SECONDS", 0)
    assert alice.get("/api/v1/releases").status_code == 401

    bob = member(app, "bob@example.com", "data")
    monkeypatch.setattr(accounts, "SESSION_CACHE_SECONDS", 30)
    assert bob.get("/api/v1/releases").status_code == 200
    assert bob.post("/api/v1/auth/logout").status_code == 204
    assert bob.get("/api/v1/releases").status_code == 401


def test_roles_first_user_owns_the_team_and_viewers_cannot_deploy(app, harness):
    h = harness
    owner = member(app, "alice@example.com", "platform")
    newcomer = member(app, "amy@example.com", "platform")
    outsider = member(app, "bob@example.com", "data")
    assert owner.get("/api/v1/access").json()["user"]["role"] == "owner"
    assert newcomer.get("/api/v1/access").json()["user"]["role"] == "viewer"
    assert outsider.get("/api/v1/access").json()["user"]["role"] == "owner"

    # A viewer reads the team's data but cannot change an app.
    denied = newcomer.post("/api/v1/releases", json=release_body(h))
    assert denied.status_code == 403 and "보기 전용" in denied.json()["detail"]
    assert newcomer.post("/api/v1/environments/test/application", json={}).status_code == 403
    assert newcomer.post("/api/v1/applications", json={}).status_code == 403
    assert newcomer.post("/api/v1/applications/a/environments", json={}).status_code == 403
    assert newcomer.get("/api/v1/environments").json()[0]["team"] is None  # nothing claimed
    release = owner.post("/api/v1/releases", json=release_body(h)).json()
    assert newcomer.get("/api/v1/releases/" + release["id"]).status_code == 200
    act = newcomer.post(f"/api/v1/releases/{release['id']}/actions", json={"action": "rollback"})
    assert act.status_code == 403

    # Everyone in the team sees its members; only an owner changes roles, only in the team.
    listed = newcomer.get("/api/v1/team").json()
    assert listed["team"] == "platform"
    assert [(m["email"], m["role"]) for m in listed["members"]] == [
        ("alice@example.com", "owner"),
        ("amy@example.com", "viewer"),
    ]
    change = {"email": "amy@example.com", "role": "member"}
    assert newcomer.post("/api/v1/team/role", json=change).status_code == 403
    assert outsider.post("/api/v1/team/role", json=change).status_code == 404
    assert owner.post("/api/v1/team/role", json={**change, "role": "admin"}).status_code == 422
    promoted = owner.post("/api/v1/team/role", json=change)
    assert promoted.status_code == 200 and promoted.json()["role"] == "member"

    # The new role applies to the signed-in session at once.
    again = newcomer.post("/api/v1/releases", json=release_body(h, "login-idempotency-key-0009"))
    assert again.status_code == 409  # allowed now; rejected only because a release is active
    assert newcomer.post("/api/v1/team/role", json=change).status_code == 403

    # A team keeps at least one owner.
    last = owner.post("/api/v1/team/role", json={"email": "alice@example.com", "role": "member"})
    assert last.status_code == 409
    assert owner.post("/api/v1/team/role", json={**change, "role": "owner"}).status_code == 200
    assert (
        owner.post(
            "/api/v1/team/role", json={"email": "alice@example.com", "role": "viewer"}
        ).status_code
        == 200
    )
    assert owner.post("/api/v1/releases", json=release_body(h)).status_code == 403


def test_users_from_before_roles_get_one_owner_per_team(harness):
    with harness.store.connect() as conn:
        for index, (email, team) in enumerate(
            [
                ("a@example.com", "platform"),
                ("b@example.com", "platform"),
                ("c@example.com", "data"),
            ]
        ):
            conn.execute(
                """INSERT INTO platform_users(id,email,password_hash,team,role,created_at)
                VALUES(%s,%s,'x',%s,NULL, now() + %s * interval '1 second')""",
                (f"user-{index}", email, team, index),
            )
    harness.store.migrate()
    harness.store.migrate()
    with harness.store.connect() as conn:
        roles = {
            row["email"]: row["role"]
            for row in conn.execute("SELECT email, role FROM platform_users").fetchall()
        }
    assert roles == {"a@example.com": "owner", "b@example.com": "member", "c@example.com": "owner"}
