"""Atomic Redis request and authentication-failure limits; no fail-open mode."""

import hashlib
import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from migration_connection.control.errors import ConnectionError
from redis.exceptions import RedisError

FAILURE = """
local blocked = tonumber(redis.call('GET', KEYS[1]) or '0')
if blocked > tonumber(ARGV[1]) then return blocked end
local n = redis.call('INCR', KEYS[2])
if n == 1 then redis.call('EXPIRE', KEYS[2], 172800) end
if n >= 20 then
  blocked = tonumber(ARGV[1]) + 86400000
  redis.call('SET', KEYS[1], blocked, 'PX', 86400000)
  return blocked
end
return 0
"""
RATE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 60) end
return n
"""


@dataclass(frozen=True)
class LimitScope:
    kind: str
    parts: tuple[str, ...]


class RedisLimiter:
    def __init__(self, client):
        self.client = client

    def _keys(self, scope, now):
        normalized = (
            json.dumps((scope.kind, *scope.parts), separators=(",", ":"))
            if isinstance(scope, LimitScope)
            else str(ipaddress.ip_address(scope))
        )
        hashed = hashlib.sha384(normalized.encode()).hexdigest()
        day = (
            datetime.fromtimestamp(now / 1000, timezone(timedelta(hours=9)))
            .date()
            .isoformat()
        )
        return "connection:block:" + hashed, "connection:failure:" + hashed + ":" + day

    def check(self, scope, now):
        try:
            block, _ = self._keys(scope, now)
            blocked = int(self.client.get(block) or 0)
            if blocked > now:
                raise ConnectionError("AUTHENTICATION_BLOCKED", 429)
            rate_key = block.replace(":block:", ":rate:") + ":" + str(now // 60000)
            if int(self.client.eval(RATE, 1, rate_key)) > 120:
                raise ConnectionError("REQUEST_RATE_LIMITED", 429)
        except ConnectionError:
            raise
        except (RedisError, OSError, ValueError, TypeError):
            raise ConnectionError("REQUEST_LIMIT_UNAVAILABLE", 503) from None

    def fail(self, scope, now):
        try:
            return int(self.client.eval(FAILURE, 2, *self._keys(scope, now), now))
        except (RedisError, OSError, ValueError, TypeError):
            raise ConnectionError("REQUEST_LIMIT_UNAVAILABLE", 503) from None

    def ready(self):
        try:
            return bool(self.client.ping())
        except (RedisError, OSError, ValueError, TypeError):
            return False

    def record_runtime(self, version, bootstrap_hash, now, tenant=""):
        runtime_key = (
            "connection:runtime:ready:" + hashlib.sha384(tenant.encode()).hexdigest()
        )
        try:
            self.client.set(
                runtime_key,
                json.dumps(
                    {
                        "version": version,
                        "bootstrap": bootstrap_hash,
                        "expires": now + 60000,
                    }
                ),
                ex=60,
            )
        except (RedisError, OSError, ValueError, TypeError):
            raise ConnectionError("REQUEST_LIMIT_UNAVAILABLE", 503) from None

    def runtime_ready(self, version, bootstrap_hash, now, tenant=""):
        runtime_key = (
            "connection:runtime:ready:" + hashlib.sha384(tenant.encode()).hexdigest()
        )
        try:
            value = json.loads(self.client.get(runtime_key) or "{}")
            return (
                value.get("version") == version
                and value.get("bootstrap") == bootstrap_hash
                and value.get("expires", 0) > now
            )
        except (RedisError, OSError, ValueError, TypeError):
            return False
