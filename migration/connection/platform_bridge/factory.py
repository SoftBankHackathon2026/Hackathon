"""Opt-in wiring. Production has no in-memory state or limiter fallback."""

import os
from pathlib import Path

from migration_connection.control.crypto import OpenSSLCrypto
from migration_connection.control.limits import RedisLimiter
from migration_connection.control.service import ConnectionService
from migration_connection.control.store import PostgresStore
from redis import Redis


def build(releases):
    if releases is None or os.getenv("ONEPORT_CONNECTION_ENABLED") != "true":
        return None
    token_file = os.environ.get("ONEPORT_CONNECTION_RUNTIME_TOKEN_FILE", "")
    token_path = Path(token_file)
    if not token_path.is_file() or token_path.stat().st_mode & 0o077:
        return None
    token = token_path.read_text().strip()
    if not token:
        return None
    redis_url = os.environ.get("ONEPORT_CONNECTION_REDIS_URL", "")
    if not redis_url:
        return None
    return ConnectionService(
        PostgresStore(releases.connect),
        OpenSSLCrypto(
            os.environ.get("ONEPORT_CONNECTION_OPENSSL", "openssl"),
            os.environ.get("ONEPORT_CONNECTION_JOB_SIGNING_KEY", ""),
        ),
        RedisLimiter(
            Redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)
        ),
        bootstrap_url=os.environ.get("ONEPORT_CONNECTION_BOOTSTRAP_URL", ""),
        release_version=os.environ.get("ONEPORT_CONNECTION_RELEASE_VERSION", ""),
        runtime_token=token,
        bootstrap_sha384=os.environ.get("ONEPORT_CONNECTION_BOOTSTRAP_SHA384", ""),
        runtime_tenant_id=os.environ.get("ONEPORT_CONNECTION_RUNTIME_TENANT_ID", ""),
        runtime_gateway_id=os.environ.get("ONEPORT_CONNECTION_RUNTIME_GATEWAY_ID", ""),
        cloud_tenants=(
            tenant.strip()
            for tenant in os.getenv("ONEPORT_CONNECTION_CLOUD_TENANTS", "").split(",")
            if tenant.strip()
        ),
    )
