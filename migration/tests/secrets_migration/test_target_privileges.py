import pytest
from migration.secrets.common import SecretsError
from migration.secrets.target import MAX_TOKEN_TTL_SECONDS, OpenBao

CONFIG = {
    "mount": "kv",
    "prefix": "migration",
    "cluster_id": "cluster-1",
    "expected_version": "2.7.2",
}
DATA = "kv/data/migration/orders/db"


class FakeBao:
    def __init__(self, extra=None, ttl=300):
        self.extra = extra or {}
        self.ttl = ttl
        self.asked = []

    def request(self, method, path, body=None):
        if path.startswith("sys/health"):
            return {
                "initialized": True,
                "sealed": False,
                "cluster_id": "cluster-1",
                "version": "2.7.2",
            }
        if path.startswith("sys/internal/ui/mounts/"):
            return {
                "data": {
                    "type": "kv",
                    "options": {"version": "2"},
                    "path": "kv/",
                    "accessor": "kv_1",
                }
            }
        if path == "auth/token/lookup-self":
            return {"data": {"policies": ["migration"], "ttl": self.ttl}}
        assert path == "sys/capabilities-self"
        self.asked = body["paths"]
        caps = {p: ["deny"] for p in body["paths"]}
        caps[DATA] = ["create", "read", "update"]
        caps.update(self.extra)
        return {"data": caps}


def test_least_privilege_token_passes_and_lifecycle_paths_are_probed():
    bao = FakeBao()
    assert (
        OpenBao(CONFIG, http=bao).inspect(["migration/orders/db"])["accessor"] == "kv_1"
    )
    assert {
        "kv/metadata/migration/orders/db",
        "kv/delete/migration/orders/db",
        "kv/destroy/migration/orders/db",
        "kv/undelete/migration/orders/db",
        "kv/data/migration__outside_secret_migration__/probe",
        "kv/data/__outside_secret_migration__/probe",
    } <= set(bao.asked)


def test_installer_metadata_read_and_list_is_allowed():
    bao = FakeBao(extra={"kv/metadata/migration/orders/db": ["list", "read"]})
    assert (
        OpenBao(CONFIG, http=bao).inspect(["migration/orders/db"])["accessor"] == "kv_1"
    )


@pytest.mark.parametrize("caps", [["update"], ["delete", "read"], ["sudo"], None])
def test_metadata_writes_are_overprivileged(caps):
    bao = FakeBao(extra={"kv/metadata/migration/orders/db": caps})
    with pytest.raises(SecretsError, match="MIGRATION_TOKEN_OVERPRIVILEGED"):
        OpenBao(CONFIG, http=bao).inspect(["migration/orders/db"])


@pytest.mark.parametrize(
    "path",
    [
        "kv/delete/migration/orders/db",
        "kv/destroy/migration/orders/db",
        "kv/data/migration__outside_secret_migration__/probe",
    ],
)
def test_extra_lifecycle_or_sibling_access_is_overprivileged(path):
    bao = FakeBao(extra={path: ["read"]})
    with pytest.raises(SecretsError, match="MIGRATION_TOKEN_OVERPRIVILEGED"):
        OpenBao(CONFIG, http=bao).inspect(["migration/orders/db"])


@pytest.mark.parametrize("ttl", [0, MAX_TOKEN_TTL_SECONDS + 1, 10 * 365 * 24 * 3600])
def test_long_lived_or_expired_tokens_are_rejected(ttl):
    with pytest.raises(SecretsError, match="FINITE_NONROOT_TOKEN_REQUIRED"):
        OpenBao(CONFIG, http=FakeBao(ttl=ttl)).inspect(["migration/orders/db"])


def test_token_at_the_ttl_limit_is_accepted():
    OpenBao(CONFIG, http=FakeBao(ttl=MAX_TOKEN_TTL_SECONDS)).inspect(
        ["migration/orders/db"]
    )
