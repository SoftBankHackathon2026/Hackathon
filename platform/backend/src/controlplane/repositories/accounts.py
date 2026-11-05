import secrets
import time
from datetime import timedelta

from psycopg.errors import UniqueViolation

from controlplane.core.errors import OperationNotFound, RequestConflict
from controlplane.core.locks import AdvisoryLock, xact_lock
from controlplane.core.security import (
    UNKNOWN_USER_HASH,
    hash_password,
    new_session_token,
    token_hash,
    verify_password,
)
from controlplane.repositories.releases import ReleaseStore
from controlplane.schemas.accounts import Credentials, Identity, Member, RoleChange, SignUp

SESSION_COOKIE = "oneport_session"
SESSION_TTL = timedelta(days=7)
SESSION_RENEW_AFTER = timedelta(hours=1)
SESSION_CACHE_SECONDS = 30


class AccountStore:
    def __init__(self, releases: ReleaseStore):
        self.connect = releases.connect
        self._verified: dict[str, tuple[float, Identity]] = {}

    def sign_up(self, request: SignUp) -> Identity:
        password_hash = hash_password(request.password)
        try:
            with self.connect() as conn:
                # Serialized, so two people creating a team at once do not both own it.
                xact_lock(conn, AdvisoryLock.CATALOG)
                first = not conn.execute(
                    "SELECT 1 FROM platform_users WHERE team=%s LIMIT 1", (request.team,)
                ).fetchone()
                role = "owner" if first else "viewer"
                conn.execute(
                    "INSERT INTO platform_users(id,email,password_hash,team,role) VALUES(%s,%s,%s,%s,%s)",
                    (secrets.token_hex(12), request.email, password_hash, request.team, role),
                )
        except UniqueViolation:
            raise RequestConflict("이미 가입된 이메일입니다.") from None
        return Identity(email=request.email, team=request.team, role=role)

    def authenticate(self, request: Credentials) -> Identity | None:
        with self.connect() as conn:
            user = conn.execute(
                "SELECT email,password_hash,team,role FROM platform_users WHERE email=%s",
                (request.email,),
            ).fetchone()
        stored = user["password_hash"] if user else UNKNOWN_USER_HASH
        if not verify_password(request.password, stored) or not user:
            return None
        return Identity(email=user["email"], team=user["team"], role=user["role"])

    def open_session(self, identity: Identity) -> str:
        token = new_session_token()
        with self.connect() as conn:
            conn.execute("DELETE FROM platform_sessions WHERE expires_at < now()")
            conn.execute(
                """INSERT INTO platform_sessions(token_hash,user_id,expires_at)
                SELECT %s, id, now() + %s FROM platform_users WHERE email=%s""",
                (token_hash(token), SESSION_TTL, identity.email),
            )
        return token

    def identity(self, token: str | None) -> Identity | None:
        if not token:
            return None
        key = token_hash(token)
        cached = self._verified.get(key)
        if cached and time.monotonic() - cached[0] < SESSION_CACHE_SECONDS:
            return cached[1]
        with self.connect() as conn:
            user = conn.execute(
                """SELECT u.email, u.team, u.role FROM platform_sessions s JOIN platform_users u ON u.id=s.user_id
                WHERE s.token_hash=%s AND s.expires_at > now()""",
                (key,),
            ).fetchone()
            if user:
                # At most one write per SESSION_RENEW_AFTER, not one per request.
                conn.execute(
                    """UPDATE platform_sessions SET expires_at = now() + %s
                    WHERE token_hash=%s AND expires_at < now() + %s""",
                    (SESSION_TTL, key, SESSION_TTL - SESSION_RENEW_AFTER),
                )
        if not user:
            self._verified.pop(key, None)
            return None
        if len(self._verified) > 10_000:
            self._verified.clear()
        identity = Identity(**user)
        self._verified[key] = (time.monotonic(), identity)
        return identity

    def close_session(self, token: str | None) -> None:
        if token:
            self._verified.pop(token_hash(token), None)
            with self.connect() as conn:
                conn.execute(
                    "DELETE FROM platform_sessions WHERE token_hash=%s", (token_hash(token),)
                )

    def members(self, team: str) -> list[Member]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT email, role, created_at FROM platform_users WHERE team=%s ORDER BY created_at, email",
                (team,),
            ).fetchall()
        return [Member(**row) for row in rows]

    def set_role(self, owner: Identity, change: RoleChange) -> Member:
        """An owner changes a teammate's role; a team always keeps at least one owner."""
        with self.connect() as conn:
            xact_lock(conn, AdvisoryLock.CATALOG)
            target = conn.execute(
                "SELECT email, role FROM platform_users WHERE email=%s AND team=%s",
                (change.email.strip().lower(), owner.team),
            ).fetchone()
            if not target:
                raise OperationNotFound("팀에 없는 사용자입니다.")
            owners = conn.execute(
                "SELECT count(*) AS n FROM platform_users WHERE team=%s AND role='owner'",
                (owner.team,),
            ).fetchone()["n"]
            if target["role"] == "owner" and change.role != "owner" and owners <= 1:
                raise RequestConflict("팀에는 관리자가 한 명 이상 있어야 합니다.")
            row = conn.execute(
                "UPDATE platform_users SET role=%s WHERE email=%s RETURNING email, role, created_at",
                (change.role, target["email"]),
            ).fetchone()
        # Signed-in sessions pick the new role up on their next request.
        self._verified.clear()
        return Member(**row)

    def owners(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT environment_id, team FROM environment_teams").fetchall()
        return {row["environment_id"]: row["team"] for row in rows}

    def claim(self, environment_id: str, identity: Identity) -> bool:
        """Make the team the owner of a free environment. True when this call claimed it."""
        with self.connect() as conn:
            created = conn.execute(
                """INSERT INTO environment_teams(environment_id,team,claimed_by) VALUES(%s,%s,%s)
                ON CONFLICT (environment_id) DO NOTHING RETURNING team""",
                (environment_id, identity.team, identity.email),
            ).fetchone()
            if created:
                return True
            owner = conn.execute(
                "SELECT team FROM environment_teams WHERE environment_id=%s", (environment_id,)
            ).fetchone()["team"]
        if owner != identity.team:
            raise RequestConflict("다른 팀이 사용 중인 환경입니다.")
        return False

    def release_claim(self, environment_id: str, identity: Identity) -> None:
        """Undo a claim whose first action failed, unless the team has deployed
        here meanwhile (a teammate's request that succeeded at the same time)."""
        with self.connect() as conn:
            conn.execute(
                """DELETE FROM environment_teams WHERE environment_id=%s AND team=%s
                AND NOT EXISTS (SELECT 1 FROM releases WHERE environment_id=%s AND team=%s)""",
                (environment_id, identity.team, environment_id, identity.team),
            )
