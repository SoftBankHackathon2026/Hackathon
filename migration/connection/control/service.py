"""Connection state machine. No product installation or arbitrary remote shell."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlsplit

from migration_connection.contracts.protocol import (
    IDENTIFIER,
    confirmation_code,
    digest,
    signed_bytes,
    unb64,
)
from migration_connection.control.errors import ConnectionError
from migration_connection.control.limits import LimitScope

RECONNECT_WAIT_MS = 90_000
CLOUD_PROVIDERS = ("aws", "gcp")
DISCOVERY_CODES = {
    "CLI_MISSING",
    "LOGIN_REQUIRED",
    "PERMISSION_DENIED",
    "REGION_REQUIRED",
    "SCOPE_LIMIT",
    "LOOKUP_FAILED",
}
CLOUD_COLLECTION_MESSAGES = {
    "LOGIN_REQUIRED": "클라우드 로그인이 만료됐어요. 단말에서 다시 로그인해 주세요.",
    "PERMISSION_DENIED": "선택한 클러스터를 조회할 권한이 없어요.",
    "GKE_AUTH_PLUGIN_MISSING": "단말에 GKE 인증 도구를 설치한 뒤 다시 수집해 주세요.",
    "SELECTED_TARGET_CHANGED_OR_MISSING": "클러스터가 바뀌었어요. 대상을 다시 확인해 주세요.",
}


PROOF_FIELDS = {
    "version",
    "request_id",
    "candidate_id",
    "challenge_id",
    "challenge",
    "device_auth_public_key",
    "wireguard_public_key",
    "audience",
    "expires_at_ms",
}
DEVICE_FIELDS = {
    "version",
    "request_id",
    "candidate_id",
    "challenge_id",
    "challenge",
    "action",
    "data",
    "expires_at_ms",
}


@dataclass(frozen=True)
class Actor:
    team: str
    email: str
    can_write: bool


def require(value, code, status=409):
    if not value:
        raise ConnectionError(code, status)


def identifier(value):
    require(
        isinstance(value, str) and bool(IDENTIFIER.fullmatch(value)),
        "INVALID_IDENTIFIER",
        422,
    )
    return value


def short_text(value, maximum=160):
    require(
        isinstance(value, str) and 0 < len(value) <= maximum and value.isprintable(),
        "INVALID_TEXT",
        422,
    )
    return value


@contextmanager
def signed_input(error_code):
    """Persist consumed nonces for malformed input, but roll back internal errors."""
    try:
        yield
    except (ValueError, TypeError, RecursionError):
        raise ConnectionError(error_code, 401) from None


def actor_scope(actor):
    return LimitScope("browser", (actor.team, actor.email))


def request_scope(request_id, *, device=False, through_tunnel=False):
    kind = "relay" if through_tunnel else "device" if device else "enrollment"
    return LimitScope(kind, (identifier(request_id),))


class ConnectionService:
    def __init__(
        self,
        store,
        crypto,
        limiter,
        *,
        bootstrap_url,
        release_version,
        runtime_token,
        bootstrap_sha384="",
        runtime_tenant_id="",
        runtime_gateway_id="",
        clock=None,
        cloud_tenants=(),
    ):
        self.store, self.crypto, self.limiter = store, crypto, limiter
        self.bootstrap_url, self.release_version = bootstrap_url, release_version
        self.runtime_token = runtime_token
        self.bootstrap_sha384 = bootstrap_sha384
        self.runtime_tenant_id = runtime_tenant_id
        self.runtime_gateway_id = runtime_gateway_id
        self.cloud_tenants = frozenset(cloud_tenants)
        self.clock = clock or (lambda: time.time_ns() // 1000000)

    def capabilities(self, team=None):
        try:
            url = urlsplit(self.bootstrap_url)
            valid_url = (
                url.scheme == "https"
                and bool(url.hostname)
                and not any(character.isspace() for character in self.bootstrap_url)
                and url.port != 0
            )
        except (ValueError, TypeError):
            valid_url = False
            url = urlsplit("")
        configured = (
            valid_url
            and url.scheme == "https"
            and bool(url.netloc)
            and not url.username
            and not url.query
            and not url.fragment
            and len(self.runtime_token) >= 32
            and bool(re.fullmatch(r"[A-Za-z0-9._-]{1,80}", self.release_version))
            and bool(re.fullmatch(r"[a-f0-9]{96}", self.bootstrap_sha384))
            and bool(self.runtime_tenant_id)
            and bool(self.runtime_gateway_id)
            and team == self.runtime_tenant_id
        )
        ready = bool(
            configured
            and self.crypto.ready()
            and self.limiter.ready()
            and self.limiter.runtime_ready(
                self.release_version,
                self.bootstrap_sha384,
                self.clock(),
                self.runtime_tenant_id,
            )
        )
        return {
            "available": ready,
            "reason": None
            if ready
            else "연결 실행 환경과 서명·요청 제한 설정을 확인해야 합니다.",
            "bootstrapUrl": self.bootstrap_url if ready else None,
            "releaseVersion": self.release_version if ready else None,
            "providers": (
                ["kubernetes", *(CLOUD_PROVIDERS if team in self.cloud_tenants else ())]
                if ready
                else []
            ),
        }

    def _touch(self, row, message):
        row["revision"] += 1
        row["events"].append({"at": self.clock(), "message": message})
        row["events"] = row["events"][-200:]

    def _access(self, row, actor, write=False):
        require(row["team"] == actor.team, "CONNECTION_NOT_FOUND", 404)
        require(not write or actor.can_write, "CONNECTION_PERMISSION_DENIED", 403)

    def _revoke(self, row):
        row["device"], row["tunnel"] = "REVOKED", "NONE"
        row["lease_expires_at"] = 0
        row["task"] = None
        row["cleanup"] = {
            "server": True,
            "gateway": False,
            "resources": True,
            "local": False,
        }

    def _replacement_gateway(self, replacement):
        if not replacement["epoch"] or replacement.get("gateway_confirmed"):
            return "ready"
        previous = self.store.get(replacement["request_id"])
        cleanup = previous.get("cleanup", {}) if previous else {}
        if cleanup.get("gateway"):
            return "ready"
        return "blocked" if cleanup.get("gateway_blocked") else "pending"

    def _expire(self, row):
        now = self.clock()
        if row["registration"] == "ISSUED" and now >= row["expires_at"]:
            row["registration"] = "EXPIRED"
        if row["device"] == "PENDING_APPROVAL" and now >= row["approval_expires_at"]:
            row["device"] = "REJECTED"
        if row["device"] == "APPROVED" and now >= row.get("device_expires_at", 0):
            self._revoke(row)
            row["issue"] = "단말 인증키 유효기간이 끝났습니다. 새로 등록하세요."
        if row["tunnel"] == "READY" and (
            now >= row.get("lease_expires_at", 0) or now >= row.get("key_expires_at", 0)
        ):
            row["tunnel"] = "DEGRADED"
            row["tunnel_checks"] = 0
            row["issue"] = (
                "연결 확인 기한이 끝났습니다. 단말에서 서비스를 확인해 주세요."
            )
        if row["tunnel"] in ("CONNECTING", "REKEYING"):
            deadline = row.get("tunnel_deadline_at")
            # Requests created before the deadline field may already be waiting.
            if deadline is None and row["epoch"] and row["events"]:
                deadline = row["events"][-1]["at"] + RECONNECT_WAIT_MS
            if deadline is not None and now >= deadline:
                row["tunnel"] = "DEGRADED"
                row["tunnel_checks"] = 0
                row["issue"] = (
                    "단말 응답을 확인하지 못했습니다. 온프레미스에서 연결 서비스를 확인해 주세요."
                )

    def snapshot(self, row, actor):
        self._access(row, actor)
        row = deepcopy(row)
        self._expire(row)
        task = row.get("task")
        collection_pending = bool(
            task
            and task["payload"]["expires_at_ms"] > self.clock()
            and task["payload"]["epoch"] == row["epoch"]
            and row["device"] == "APPROVED"
            and row["tunnel"] == "READY"
        )
        result = {
            "id": row["id"],
            "revision": row["revision"],
            "organization": row["team"],
            "target": row.get("target"),
            "provider": row.get("provider", "kubernetes"),
            "serverNow": self.clock(),
            "observedAt": self.clock(),
            "registrationExpiresAt": row["expires_at"],
            "registration": row["registration"],
            "device": row["device"],
            "tunnel": row["tunnel"],
            "collection": row["collection"],
            "collectionPending": collection_pending,
            "discovered": row["discovered"],
            "collected": row["collected"],
            "workloads": deepcopy(row.get("inventory_report", {}).get("workloads"))
            if row["device"] == "APPROVED"
            and row.get("inventory_epoch") == row["epoch"]
            else None,
            "permissions": {
                "approveDevice": actor.can_write,
                "registerEnvironment": actor.can_write,
                "revoke": actor.can_write,
            },
            "attemptsLeft": row["attempts_left"],
            "tunnelChecks": row["tunnel_checks"],
            "epoch": row["epoch"],
            "events": row["events"],
            "deployment": {
                "bootstrapUrl": self.bootstrap_url,
                "releaseVersion": self.release_version,
            },
        }
        if result["provider"] in CLOUD_PROVIDERS:
            result.update(
                discoveryGeneration=row.get("discovery_generation", 0),
                discoveryComplete=(
                    row.get("discovery_epoch") == row["epoch"]
                    and row["tunnel"] == "READY"
                ),
                discoveryWarnings=row.get("discovery_warnings", []),
            )
        for internal, external in [
            ("approval_expires_at", "approvalExpiresAt"),
            ("collected_at", "collectedAt"),
            ("inventory_digest", "inventoryDigest"),
            ("selected_id", "selectedEnvironmentId"),
            ("environment_id", "environmentId"),
            ("registered_at", "registeredAt"),
            ("key_expires_at", "keyExpiresAt"),
            ("last_tunnel_activity_at", "lastTunnelActivityAt"),
            ("cleanup", "cleanup"),
            ("issue", "issue"),
        ]:
            if internal in row:
                result[external] = row[internal]
        if replacement := row.get("replacement"):
            result["replacement"] = {
                "previousRequestId": replacement["request_id"],
                "gateway": self._replacement_gateway(replacement),
                "client": replacement.get("client", "pending"),
            }
        if row.get("candidate"):
            result["candidate"] = {
                k: row["candidate"][k]
                for k in ("id", "digest", "fingerprint", "hostname", "os", "arch")
            }
            result["candidate"]["supportsReplacement"] = row["candidate"].get(
                "supports_replacement", False
            )
        return result

    def list(self, actor):
        return [self.snapshot(row, actor) for row in self.store.list(actor.team)]

    def get(self, request_id, actor):
        row = self.store.get(identifier(request_id))
        require(row is not None, "CONNECTION_NOT_FOUND", 404)
        return self.snapshot(row, actor)

    def create(self, actor, public_key, operation_id, provider="kubernetes"):
        require(actor.can_write, "CONNECTION_PERMISSION_DENIED", 403)
        self.limiter.check(actor_scope(actor), self.clock())
        capability = self.capabilities(actor.team)
        require(capability["available"], "CONNECTION_UNAVAILABLE", 503)
        require(provider in capability["providers"], "PROVIDER_UNAVAILABLE", 422)
        try:
            unb64(public_key, 1952)
        except ValueError:
            raise ConnectionError("INVALID_ENROLLMENT_PUBLIC_KEY", 422) from None
        require(
            isinstance(operation_id, str)
            and bool(re.fullmatch(r"[A-Za-z0-9_-]{16,80}", operation_id)),
            "INVALID_OPERATION_ID",
            422,
        )
        now = self.clock()
        row = self.store.create(
            {
                "id": secrets.token_urlsafe(16),
                "team": actor.team,
                "owner": actor.email,
                "operation_id": operation_id,
                "public_key": public_key,
                "provider": provider,
                "expires_at": now + 60000,
                "registration": "ISSUED",
                "device": "NONE",
                "tunnel": "NONE",
                "collection": "WAITING",
                "revision": 1,
                "discovered": [],
                "collected": [],
                "epoch": 0,
                "tunnel_checks": 0,
                "attempts_left": 5,
                "events": [{"at": now, "message": "단말 등록 요청을 만들었습니다."}],
                "challenges": {},
                "device_challenges": {},
            }
        )
        require(
            row["public_key"] == public_key
            and row.get("provider", "kubernetes") == provider,
            "IDEMPOTENCY_CONFLICT",
        )
        return self.snapshot(row, actor)

    def challenge(self, request_id, candidate_id, *, device=False):
        now = self.clock()
        identifier(candidate_id)
        with self.store.locked(identifier(request_id)) as row:
            scope = (
                LimitScope("device-challenge", (request_id,))
                if device
                else request_scope(request_id)
            )
            self.limiter.check(scope, now)
            self._expire(row)
            if device:
                require(
                    row.get("candidate", {}).get("id") == candidate_id
                    and row["device"]
                    in ("PENDING_APPROVAL", "APPROVED", "REVOKED", "REJECTED"),
                    "DEVICE_UNAVAILABLE",
                    403,
                )
                expiry = now + 30000
            else:
                require(row["registration"] == "ISSUED", "ENROLLMENT_UNAVAILABLE", 403)
                expiry = min(row["expires_at"], now + 30000)
            bucket = "device_challenges" if device else "challenges"
            row[bucket] = {
                k: v for k, v in row[bucket].items() if v["expires_at_ms"] > now
            }
            require(len(row[bucket]) < 8, "TOO_MANY_CHALLENGES", 429)
            value = {
                "challenge_id": secrets.token_urlsafe(16),
                "challenge": secrets.token_urlsafe(32),
                "expires_at_ms": expiry,
                "candidate_id": candidate_id,
            }
            row[bucket][value["challenge_id"]] = value
            return {k: v for k, v in value.items() if k != "candidate_id"}

    def _nonce(self, row, payload, *, device=False):
        bucket = row["device_challenges" if device else "challenges"]
        challenge_id = payload.get("challenge_id")
        challenge = bucket.get(challenge_id) if isinstance(challenge_id, str) else None
        require(
            challenge is not None
            and challenge["candidate_id"] == payload.get("candidate_id")
            and challenge["challenge"] == payload.get("challenge")
            and type(payload.get("expires_at_ms")) is int
            and challenge["expires_at_ms"] == payload["expires_at_ms"]
            and self.clock() < challenge["expires_at_ms"],
            "INVALID_CHALLENGE",
            401,
        )
        del bucket[challenge_id]

    def prove(self, request_id, body):
        now = self.clock()
        scope = request_scope(request_id)
        consumed = False
        try:
            with self.store.locked(request_id) as row:
                self.limiter.check(scope, now)
                self._expire(row)
                require(row["registration"] == "ISSUED", "ENROLLMENT_UNAVAILABLE", 409)
                p = body["payload"]
                require(
                    isinstance(p, dict)
                    and set(p) - {"replacement_version"} == PROOF_FIELDS
                    and (
                        "replacement_version" not in p
                        or (
                            type(p["replacement_version"]) is int
                            and p["replacement_version"] == 1
                        )
                    )
                    and type(p["version"]) is int
                    and p["version"] == 1
                    and p["request_id"] == request_id
                    and p["audience"] == "migration-connector-enrollment",
                    "INVALID_PROOF",
                    401,
                )
                identifier(p["candidate_id"])
                self._nonce(row, p)
                consumed = True
                with signed_input("INVALID_PROOF"):
                    device_key = unb64(p["device_auth_public_key"], 1952)
                    unb64(p["wireguard_public_key"], 32)
                    require(
                        self.crypto.verify(
                            row["public_key"],
                            body["enrollment_signature"],
                            signed_bytes("enrollment", p),
                        )
                        and self.crypto.verify(
                            p["device_auth_public_key"],
                            body["device_signature"],
                            signed_bytes("device-possession", p),
                        ),
                        "INVALID_PROOF",
                        401,
                    )
                meta = body.get("metadata", {})
                require(
                    isinstance(meta, dict) and set(meta) <= {"hostname", "os", "arch"},
                    "INVALID_METADATA",
                    422,
                )
                row["candidate"] = {
                    "id": p["candidate_id"],
                    "digest": digest(p),
                    "fingerprint": hashlib.sha384(device_key).hexdigest(),
                    "hostname": short_text(meta.get("hostname", "unknown")),
                    "os": short_text(meta.get("os", "unknown")),
                    "arch": short_text(meta.get("arch", "unknown")),
                    "supports_replacement": p.get("replacement_version") == 1,
                    "public_key": p["device_auth_public_key"],
                    "wireguard_public_key": p["wireguard_public_key"],
                }
                row["device_id"] = secrets.token_urlsafe(16)
                row["code"] = confirmation_code(p)
                row["registration"] = "CONSUMED"
                row["device"] = "PENDING_APPROVAL"
                row["approval_expires_at"] = now + 300000
                row["challenges"] = {}
                self._touch(row, "단말 등록 증명을 확인했습니다. 웹 승인을 기다립니다.")
                return {
                    "device_id": row["device_id"],
                    "candidate_digest": row["candidate"]["digest"],
                    "confirmation_code": row["code"],
                    "approval_expires_at_ms": row["approval_expires_at"],
                }
        except ConnectionError as error:
            if consumed and error.status == 401:
                self.limiter.fail(scope, now)
            raise

    def browser(self, request_id, actor, action, data):
        self.limiter.check(actor_scope(actor), self.clock())
        request_id = identifier(request_id)
        replace_id = data.get("replaceId") if action == "approve" else None
        if replace_id is not None:
            replace_id = identifier(replace_id)
            require(replace_id != request_id, "REPLACEMENT_UNAVAILABLE")
        with self.store.locked_rows(
            *([request_id, replace_id] if replace_id else [request_id])
        ) as rows:
            row = rows[request_id]
            self._access(row, actor, True)
            previous = rows.get(replace_id)
            if previous is not None:
                self._access(previous, actor, True)
            self._expire(row)
            if action == "cancel":
                require(
                    row["registration"] in ("ISSUED", "EXPIRED", "CANCELLED"),
                    "ALREADY_CONSUMED",
                )
                row["registration"] = "CANCELLED"
                row["challenges"] = {}
            elif action in ("approve", "reject"):
                if (
                    action == "approve"
                    and previous is not None
                    and row["device"] == "APPROVED"
                    and row.get("replacement", {}).get("request_id") == replace_id
                ):
                    return self.snapshot(row, actor)
                require(row["device"] == "PENDING_APPROVAL", "APPROVAL_UNAVAILABLE")
                if action == "reject":
                    row["device"] = "REJECTED"
                else:
                    require(
                        data.get("candidateDigest") == row["candidate"]["digest"],
                        "CANDIDATE_CHANGED",
                    )
                    code = data.get("code")
                    code = (
                        re.sub(r"[\s-]", "", code).upper()
                        if isinstance(code, str)
                        else ""
                    )
                    if not re.fullmatch(
                        r"[A-Z2-7]{8}", code
                    ) or not hmac.compare_digest(code, row["code"]):
                        row["attempts_left"] -= 1
                        if row["attempts_left"] <= 0:
                            row["device"] = "REJECTED"
                        self._touch(row, "단말 확인 코드가 일치하지 않습니다.")
                        raise ConnectionError("INVALID_CONFIRMATION_CODE", 422)
                    if previous is not None:
                        require(
                            row["candidate"].get("supports_replacement"),
                            "REPLACEMENT_CLIENT_UNSUPPORTED",
                        )
                        self._expire(previous)
                        require(
                            previous["device"] == "APPROVED"
                            and (
                                not previous.get("replacement")
                                or previous["replacement"].get("client") == "ready"
                            ),
                            "REPLACEMENT_UNAVAILABLE",
                        )
                        row["replacement"] = {
                            "request_id": previous["id"],
                            "epoch": previous["epoch"],
                            "connection_id": previous.get("connection_id"),
                        }
                        self._revoke(previous)
                        if previous["epoch"] == 0:
                            previous["cleanup"]["gateway"] = True
                        self._touch(
                            previous, "새 단말 연결 승인으로 기존 연결을 철회했습니다."
                        )
                    row["device"], row["tunnel"] = "APPROVED", "CONNECTING"
                    row["device_expires_at"] = self.clock() + 90 * 86400000
                    row["tunnel_deadline_at"] = self.clock() + 300_000
            elif action == "revoke":
                if row["device"] != "REVOKED":
                    self._revoke(row)
            elif action == "reconnect":
                require(row["device"] == "APPROVED", "RECONNECT_UNAVAILABLE")
                if row["tunnel"] in ("READY", "CONNECTING", "REKEYING"):
                    return self.snapshot(row, actor)
                require(row["tunnel"] == "DEGRADED", "RECONNECT_UNAVAILABLE")
                row["tunnel"] = "CONNECTING"
                row["tunnel_checks"] = 0
                row["tunnel_deadline_at"] = self.clock() + RECONNECT_WAIT_MS
                row["reconnect_after_epoch"] = row["epoch"]
                row["task"] = None
                row.pop("issue", None)
            elif action == "rediscover":
                self._ready(row)
                require(
                    row.get("provider") in CLOUD_PROVIDERS
                    and not row.get("environment_id")
                    and row.get("discovery_epoch") == row["epoch"]
                    and not row.get("task"),
                    "DISCOVERY_UNAVAILABLE",
                )
                row["discovery_generation"] = row.get("discovery_generation", 0) + 1
                row["discovered"] = []
                row.pop("discovery_epoch", None)
                row.pop("discovery_warnings", None)
                row.pop("issue", None)
            elif action == "collect":
                self._ready(row)
                require(
                    not row.get("environment_id")
                    or data.get("discoveredId") == row.get("selected_id"),
                    "ALREADY_REGISTERED",
                )
                selected = next(
                    (
                        e
                        for e in row["discovered"]
                        if e["id"] == data.get("discoveredId")
                    ),
                    None,
                )
                require(selected is not None, "TARGET_NOT_DISCOVERED")
                require(
                    not row.get("task")
                    or self.clock() >= row["task"]["payload"]["expires_at_ms"],
                    "COLLECTION_PENDING",
                )
                task = {
                    "job_id": secrets.token_urlsafe(16),
                    "operation": "environment.inspect",
                    "device_id": row["device_id"],
                    "request_id": row["id"],
                    "epoch": row["epoch"],
                    "target_id": selected["id"],
                    "expires_at_ms": self.clock() + 120000,
                }
                if row.get("provider") in CLOUD_PROVIDERS:
                    require(
                        row.get("discovery_epoch") == row["epoch"],
                        "TARGET_NOT_DISCOVERED",
                    )
                    task["cloud_context"] = selected["context"]
                row.pop("issue", None)
                row["task"] = {
                    "payload": task,
                    "signature": self.crypto.sign(signed_bytes("job", task)),
                }
                row["collection"] = "WAITING"
                row["collected"] = []
                row.pop("inventory_digest", None)
                row.pop("inventory_report", None)
                row["selected_id"] = selected["id"]
            elif action == "register":
                self._ready(row)
                require(not row.get("environment_id"), "ALREADY_REGISTERED")
                require(
                    row["collection"] == "COLLECTED"
                    and data.get("inventoryDigest") == row.get("inventory_digest")
                    and data.get("collectedId") == row.get("selected_id")
                    and row.get("inventory_epoch") == row["epoch"],
                    "INVENTORY_CHANGED",
                )
                target = data.get("target", {})
                selected = row["collected"][0]
                require(
                    isinstance(target, dict)
                    and set(target) - {"role"}
                    == {"organization", "project", "name", "kind", "region"}
                    and target["organization"] == row["team"]
                    and target["kind"] == selected["kind"]
                    and target["region"] == selected["region"]
                    and (
                        "role" not in target or target["role"] in ("source", "target")
                    ),
                    "INVALID_REGISTRATION",
                    422,
                )
                short_text(target["name"], 100)
                short_text(target["project"], 100)
                row["target"] = deepcopy(target)
                row["environment_id"] = "connected-" + row["id"]
                row["registered_at"] = self.clock()
            else:
                raise ConnectionError("UNSUPPORTED_ACTION", 422)
            self._touch(
                row,
                {
                    "cancel": "미사용 등록 요청을 취소했습니다.",
                    "approve": "단말 연결을 승인했습니다.",
                    "reject": "단말 후보를 거절했습니다.",
                    "revoke": "서버 권한을 철회했습니다. 정리 확인을 기다립니다.",
                    "reconnect": "새 연결을 요청했습니다.",
                    "collect": "선택한 환경정보 수집을 요청했습니다.",
                    "rediscover": "정보수집 대상을 다시 확인합니다.",
                    "register": "환경정보를 등록했습니다.",
                }[action],
            )
            return self.snapshot(row, actor)

    def _ready(self, row):
        self._expire(row)
        require(
            row["device"] == "APPROVED" and row["tunnel"] == "READY", "TUNNEL_NOT_READY"
        )

    def device(self, request_id, body, *, through_tunnel=False):
        now = self.clock()
        scope = request_scope(request_id, device=True, through_tunnel=through_tunnel)
        consumed = False
        try:
            with self.store.locked(request_id) as row:
                self.limiter.check(scope, now)
                self._expire(row)
                if through_tunnel:
                    require(
                        row["team"] == self.runtime_tenant_id,
                        "RUNTIME_TENANT_MISMATCH",
                        403,
                    )
                p = body["payload"]
                require(
                    isinstance(p, dict)
                    and set(p) == DEVICE_FIELDS
                    and type(p["version"]) is int
                    and p["version"] == 1
                    and p["request_id"] == request_id
                    and p["candidate_id"] == row.get("candidate", {}).get("id"),
                    "INVALID_DEVICE_REQUEST",
                    401,
                )
                require(
                    row["device"]
                    in ("PENDING_APPROVAL", "APPROVED", "REVOKED", "REJECTED"),
                    "DEVICE_UNAVAILABLE",
                    403,
                )
                self._nonce(row, p, device=True)
                consumed = True
                with signed_input("INVALID_DEVICE_REQUEST"):
                    require(
                        self.crypto.verify(
                            row["candidate"]["public_key"],
                            body["signature"],
                            signed_bytes("device-request", p),
                        ),
                        "INVALID_DEVICE_SIGNATURE",
                        401,
                    )
                action, data = p["action"], p["data"]
                require(isinstance(data, dict), "INVALID_DEVICE_DATA", 422)
                actor = Actor(row["team"], row["owner"], False)
                if action == "status":
                    return {
                        "snapshot": self.snapshot(row, actor),
                        "device_id": row["device_id"],
                        "confirmation_code": row["code"]
                        if row["device"] == "PENDING_APPROVAL"
                        else None,
                        "replacement": row.get("replacement")
                        if row["device"] == "APPROVED"
                        else None,
                    }
                if action == "replacement-cleanup":
                    replacement = row.get("replacement")
                    require(
                        row["device"] == "APPROVED"
                        and replacement
                        and set(data) == {"client"}
                        and data["client"] in ("ready", "blocked"),
                        "INVALID_CLEANUP",
                        422,
                    )
                    require(
                        self._replacement_gateway(replacement) == "ready",
                        "REPLACEMENT_GATEWAY_PENDING",
                    )
                    if (
                        replacement.get("client") != "ready"
                        and replacement.get("client") != data["client"]
                    ):
                        replacement["client"] = data["client"]
                        self._touch(
                            row, "새 연결을 위한 단말 터널 정리 상태를 확인했습니다."
                        )
                    return {"accepted": True}
                if action == "cleanup":
                    require(
                        row["device"] in ("REVOKED", "REJECTED")
                        and set(data) == {"local"}
                        and data["local"] is True,
                        "INVALID_CLEANUP",
                        422,
                    )
                    row.setdefault(
                        "cleanup",
                        {
                            "server": True,
                            "gateway": True,
                            "resources": True,
                            "local": False,
                        },
                    )
                    row["cleanup"]["local"] = True
                    self._touch(row, "단말 정리를 확인했습니다.")
                    return {"accepted": True}
                require(through_tunnel, "TUNNEL_TRANSPORT_REQUIRED", 403)
                self._ready(row)
                if action == "poll":
                    row["last_tunnel_activity_at"] = max(
                        now, row.get("last_tunnel_activity_at", 0)
                    )
                    task = row.get("task")
                    if task and now >= task["payload"]["expires_at_ms"]:
                        row["task"] = None
                        row["issue"] = (
                            "환경정보 수집 기한이 끝났습니다. 다시 요청하세요."
                        )
                        task = None
                    return {"snapshot": self.snapshot(row, actor), "task": task}
                if action == "discovered":
                    provider = row.get("provider", "kubernetes")
                    cloud = provider in CLOUD_PROVIDERS
                    require(
                        set(data)
                        == (
                            {"environments", "warnings", "generation", "epoch"}
                            if cloud
                            else {"environments"}
                        )
                        and isinstance(data["environments"], list)
                        and len(data["environments"]) <= 100,
                        "INVALID_DISCOVERY",
                        422,
                    )
                    found, ids = [], set()
                    for env in data["environments"]:
                        require(
                            isinstance(env, dict)
                            and set(env)
                            == {"id", "kind", "name", "identity", "region", "context"}
                            and env["kind"] == provider,
                            "INVALID_DISCOVERY",
                            422,
                        )
                        for field in ("id", "name", "identity", "context"):
                            short_text(env[field], 512)
                        require(env["id"] not in ids, "DUPLICATE_DISCOVERY", 422)
                        ids.add(env["id"])
                        require(
                            isinstance(env["region"], str)
                            and len(env["region"]) <= 160,
                            "INVALID_REGION",
                            422,
                        )
                        found.append(deepcopy(env))
                    if cloud:
                        require(
                            type(data["generation"]) is int
                            and data["generation"] == row.get("discovery_generation", 0)
                            and type(data["epoch"]) is int
                            and data["epoch"] == row["epoch"],
                            "STALE_DISCOVERY",
                        )
                        warnings = data["warnings"]
                        require(
                            isinstance(warnings, list) and len(warnings) <= 20,
                            "INVALID_DISCOVERY",
                            422,
                        )
                        for warning in warnings:
                            require(
                                isinstance(warning, dict)
                                and set(warning) == {"code", "scope"}
                                and warning["code"] in DISCOVERY_CODES
                                and isinstance(warning["scope"], str)
                                and bool(
                                    re.fullmatch(
                                        r"[A-Za-z0-9_/-]{0,230}", warning["scope"]
                                    )
                                ),
                                "INVALID_DISCOVERY",
                                422,
                            )
                        row["discovery_warnings"] = deepcopy(warnings)
                        row["discovery_epoch"] = row["epoch"]
                    row["discovered"] = found
                    self._touch(row, "단말의 정보수집 대상을 확인했습니다.")
                    return {"accepted": True}
                if action in ("inventory", "inventory-failed"):
                    from migration_connection.inventory import (
                        inventory_digest,
                        validate_report,
                    )

                    failed = action == "inventory-failed"
                    require(
                        set(data) == {"job_id", "reason" if failed else "report"},
                        "INVALID_INVENTORY",
                        422,
                    )
                    task = row.get("task")
                    require(
                        task is not None
                        and data["job_id"] == task["payload"]["job_id"]
                        and task["payload"]["epoch"] == row["epoch"]
                        and now < task["payload"]["expires_at_ms"],
                        "STALE_COLLECTION",
                    )
                    if failed:
                        require(
                            row.get("provider") in CLOUD_PROVIDERS
                            and isinstance(data["reason"], str),
                            "INVALID_INVENTORY",
                            422,
                        )
                        row["task"] = None
                        row["issue"] = CLOUD_COLLECTION_MESSAGES.get(
                            data["reason"],
                            "클러스터에 접속하지 못했어요. 네트워크와 Kubernetes 조회 권한을 확인해 주세요.",
                        )
                        self._touch(row, "선택한 클러스터를 수집하지 못했습니다.")
                        return {"accepted": True}
                    try:
                        validate_report(
                            data["report"],
                            expected_request_id=row["id"],
                            expected_device_id=row["device_id"],
                            expected_epoch=row["epoch"],
                            expected_target_id=task["payload"]["target_id"],
                        )
                        report = data["report"]
                        require(
                            report["environment"]["kind"]
                            == row.get("provider", "kubernetes"),
                            "INVALID_INVENTORY",
                            422,
                        )
                        if row.get("provider") in CLOUD_PROVIDERS:
                            require(
                                report["environment"]["context"]
                                == task["payload"]["cloud_context"],
                                "INVALID_INVENTORY",
                                422,
                            )
                        require(
                            abs(now - report["collected_at_ms"]) <= 60000,
                            "STALE_COLLECTION",
                        )
                        value = inventory_digest(report)
                    except (ValueError, TypeError, KeyError):
                        raise ConnectionError("INVALID_INVENTORY", 422) from None
                    row["collected"] = [deepcopy(report["environment"])]
                    row["inventory_digest"] = value
                    row["inventory_epoch"] = row["epoch"]
                    row["collected_at"] = now
                    row["collection"] = "COLLECTED"
                    row["inventory_report"] = deepcopy(report)
                    row["task"] = None
                    row.pop("issue", None)
                    self._touch(row, "선택한 환경정보 수집을 완료했습니다.")
                    return {"accepted": True, "inventory_digest": value}
                raise ConnectionError("UNSUPPORTED_DEVICE_ACTION", 422)
        except ConnectionError as error:
            if consumed and error.status == 401:
                self.limiter.fail(scope, now)
            raise

    def runtime_health(self, data):
        require(
            set(data)
            == {
                "release_version",
                "protocol_version",
                "crypto_profile",
                "bootstrap_sha384",
                "tenant_id",
                "gateway_id",
            }
            and data["release_version"] == self.release_version
            and data["protocol_version"] == 1
            and data["crypto_profile"] == "MLDSA65-X25519MLKEM768-WG"
            and data["bootstrap_sha384"] == self.bootstrap_sha384
            and data["tenant_id"] == self.runtime_tenant_id
            and data["gateway_id"] == self.runtime_gateway_id,
            "RUNTIME_RELEASE_MISMATCH",
            422,
        )
        self.limiter.record_runtime(
            self.release_version,
            self.bootstrap_sha384,
            self.clock(),
            self.runtime_tenant_id,
        )
        return {"accepted": True, "expires_at_ms": self.clock() + 60000}

    def authorize(self, request_id):
        with self.store.locked(identifier(request_id)) as row:
            self._expire(row)
            require(
                row["team"] == self.runtime_tenant_id, "RUNTIME_TENANT_MISMATCH", 403
            )
            require(row["device"] == "APPROVED", "DEVICE_NOT_APPROVED", 403)
            return {
                "request_id": row["id"],
                "tenant_id": row["team"],
                "device_id": row["device_id"],
                "device_auth_public_key": row["candidate"]["public_key"],
                "wireguard_public_key": row["candidate"]["wireguard_public_key"],
                "epoch": row["epoch"],
                "state": row["tunnel"],
                "authorization_expires_at_ms": self.clock() + 60000,
                "replacement": row.get("replacement")
                if row.get("replacement")
                and self._replacement_gateway(row["replacement"]) != "ready"
                else None,
            }

    def tunnel(self, request_id, data):
        with self.store.locked(identifier(request_id)) as row:
            self._expire(row)
            require(
                row["team"] == self.runtime_tenant_id, "RUNTIME_TENANT_MISMATCH", 403
            )
            if data.get("state") in ("REVOKED", "REVOKE_BLOCKED"):
                require(row["device"] == "REVOKED", "DEVICE_NOT_REVOKED")
                if row["epoch"]:
                    require(
                        data.get("epoch") == row["epoch"]
                        and data.get("connection_id") == row.get("connection_id"),
                        "CONNECTION_BINDING_CHANGED",
                    )
                cleanup = row["cleanup"]
                if not cleanup["gateway"]:
                    if data["state"] == "REVOKED":
                        cleanup["gateway"] = True
                        cleanup.pop("gateway_blocked", None)
                        self._touch(row, "게이트웨이 터널 차단을 확인했습니다.")
                    elif not cleanup.get("gateway_blocked"):
                        cleanup["gateway_blocked"] = True
                        self._touch(
                            row, "게이트웨이에서 기존 터널을 정리하지 못했습니다."
                        )
                return {"accepted": True}
            require(row["device"] == "APPROVED", "DEVICE_NOT_APPROVED", 403)
            now = self.clock()
            epoch = data.get("epoch")
            require(
                type(epoch) is int
                and row["epoch"] <= epoch <= row["epoch"] + 1
                and epoch >= 1,
                "INVALID_KEY_EPOCH",
            )
            identifier(data.get("connection_id"))
            state = data.get("state")
            require(
                state in ("READY", "DEGRADED", "REKEYING"), "INVALID_TUNNEL_STATE", 422
            )
            if replacement := row.get("replacement"):
                require(
                    self._replacement_gateway(replacement) == "ready",
                    "REPLACEMENT_GATEWAY_PENDING",
                )
                replacement["gateway_confirmed"] = True
                if state == "READY":
                    require(
                        replacement.get("client") == "ready",
                        "REPLACEMENT_CLIENT_PENDING",
                    )
            if epoch == row["epoch"]:
                require(
                    data["connection_id"] == row.get("connection_id"),
                    "CONNECTION_BINDING_CHANGED",
                )
            else:
                row["task"] = None
                if not row.get("environment_id"):
                    row["collection"] = "WAITING"
                    row["collected"] = []
                    row.pop("inventory_digest", None)
            if state == "READY":
                require(
                    epoch > row.get("reconnect_after_epoch", 0),
                    "NEW_KEY_EPOCH_REQUIRED",
                )
                require(data.get("checks") == 2, "TUNNEL_PROBES_REQUIRED")
                expires, lease = (
                    data.get("key_expires_at_ms"),
                    data.get("lease_expires_at_ms"),
                )
                require(
                    type(expires) is int
                    and now < expires <= now + 1800000
                    and type(lease) is int
                    and now < lease <= min(expires, now + 60000),
                    "INVALID_TUNNEL_EXPIRY",
                )
                # A heartbeat may renew only the authorization lease, never the key lifetime.
                if epoch == row["epoch"] and row.get("key_expires_at"):
                    require(expires == row["key_expires_at"], "KEY_LIFETIME_CHANGED")
                if epoch != row["epoch"] or not row.get("key_expires_at"):
                    row["last_tunnel_activity_at"] = now
                row["key_expires_at"], row["lease_expires_at"] = expires, lease
                row["tunnel_checks"] = 4
                row.pop("tunnel_deadline_at", None)
                row.pop("reconnect_after_epoch", None)
                row.pop("issue", None)
            else:
                row["lease_expires_at"] = 0
                if epoch != row["epoch"]:
                    # The first READY in a new generation establishes its own
                    # lifetime. Same-generation REKEYING must retain the bound.
                    row["key_expires_at"] = 0
                    row["tunnel_checks"] = 0
            if state == "REKEYING" and epoch != row["epoch"]:
                row["tunnel_deadline_at"] = now + RECONNECT_WAIT_MS
            if state == "DEGRADED":
                row["tunnel_checks"] = 0
                row["issue"] = (
                    "터널 통신이 끊겼습니다. 온프레미스 단말에서 연결 서비스를 확인해 주세요."
                )
            changed = row["tunnel"] != state or row["epoch"] != epoch
            row["epoch"], row["connection_id"], row["tunnel"] = (
                epoch,
                data["connection_id"],
                state,
            )
            if changed:
                self._touch(row, "보안 연결 상태를 확인했습니다: " + state)
            return {
                "accepted": True,
                "authorization_expires_at_ms": row.get("lease_expires_at", 0),
            }
