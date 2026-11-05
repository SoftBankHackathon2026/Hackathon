"""Pinned REDIS_URL and TLS CA connections for the existing Memorystore provisioner.

Persist intent before publishing a Secret version. Lost
responses are recovered by observation, without resubmission or reading Secrets.
Only pinned Secret references and public resource identities enter the journal.
"""

import hashlib
import ipaddress
import re
from enum import StrEnum
from urllib.parse import quote, urlencode

from controlplane.execution.gcp.api import ApiBacked, ApiProfile, GcpApiClient
from controlplane.execution.gcp.cache import OWNER, REDIS_API, cache_status
from controlplane.execution.gcp.errors import DataProvisionError
from controlplane.execution.gcp.journal import (
    JournalTable,
    read_row,
    save_checkpoint,
)
from controlplane.execution.gcp.secret_manager import SECRETS_API, SecretManager, labels_match
from controlplane.providers.contracts import ProviderConflict
from controlplane.providers.gcp.constants import REDIS_CA_DIR, REDIS_CA_FILE, REDIS_CA_VOLUME

__all__ = [
    "GcpRedisConnector",
    "REDIS_CA_DIR",
    "REDIS_CA_FILE",
    "REDIS_CA_VOLUME",
    "redis_status",
]

MAX_MISSING_VERSION_OBSERVATIONS = 12
VERSION_NUMBER = re.compile(r"[1-9][0-9]*")

API_PROFILE = ApiProfile(
    label="GCP Redis API",
    rejected=(
        "GCP Redis 준비 API 요청 실패(HTTP {status}). "
        "캐시 workflow·배포 계정 권한·API·할당량을 확인하세요."
    ),
    failure=DataProvisionError,
)


class SecretKind(StrEnum):
    CA = "ca"
    URL = "url"


redis_status = cache_status


class GcpRedisConnector(ApiBacked):
    def __init__(self, session, settings):
        self.env, self.policy = settings.environment, settings.cache_provisioning
        self.api = GcpApiClient(session, API_PROFILE)
        self.secrets = SecretManager(self.api, self.env.project_id, self.env.region)
        parent = f"projects/{self.env.project_id}/locations/{self.env.region}"
        self.instance = f"{parent}/instances/oneport-cache-{self.policy.app_id}"
        self.owner = hashlib.sha256(
            f"{self.env.service_name}/{settings.resource_owner}".encode()
        ).hexdigest()[:32]
        self.labels = {
            "managed_by": "oneport-platform-cache",
            "app_id": self.policy.app_id,
            "oneport_owner": self.owner,
        }
        self.fingerprint = hashlib.sha256(
            (settings.resource_owner + self.policy.fingerprint()).encode()
        ).hexdigest()
        self.secret_names = {
            kind: f"projects/{self.env.project_id}/secrets/oneport_{self.policy.app_id}_redis_{kind}"
            for kind in SecretKind
        }

    def binding(self, version="1", ca_version="1"):
        return {
            "secret": self.secret_names[SecretKind.URL],
            "version": version,
            "ca_secret": self.secret_names[SecretKind.CA],
            "ca_version": ca_version,
            "service_account": f"oneport-rt-{self.policy.app_id}@{self.env.project_id}.iam.gserviceaccount.com",
            "network": self.policy.network,
            "subnetwork": self.policy.subnetwork,
        }

    def step(self, conn):
        row = read_row(conn, JournalTable.CACHE, self.env.service_name)
        if not row or row["policy_fingerprint"] != self.fingerprint:
            raise DataProvisionError("등록된 Redis 준비 작업을 확인할 수 없습니다.")
        cp = dict(row["checkpoint"])
        if cp.get("phase") != "ready":
            raise DataProvisionError("Redis 생성·CACHE_CONFIG 준비가 완료되지 않았습니다.")
        instance = self.api.call_unless_missing("GET", REDIS_API + self.instance)
        if instance is None:
            raise DataProvisionError("사용 중인 Redis 인스턴스가 사라졌습니다.")
        if not self._instance_ready(instance, cp):
            return "instance", None
        cp["create_time"] = instance["createTime"]
        cp["redis_phase"] = "connection"
        self._save(conn, cp)
        for kind in (SecretKind.CA, SecretKind.URL):
            if not self._publish(conn, cp, kind, instance):
                return "connection", None
        cp["redis_phase"] = "ready"
        self._save(conn, cp)
        return "ready", self.binding(cp["url_version"], cp["ca_version"])

    def _save(self, conn, cp):
        save_checkpoint(conn, JournalTable.CACHE, self.env.service_name, cp)

    def _instance_ready(self, instance, cp):
        labels = instance.get("labels", {})
        if (
            instance.get("name") != self.instance
            or labels.get("managed_by") != OWNER
            or labels.get("app_id") != self.policy.app_id
            or instance.get("authorizedNetwork") != self.policy.network
            or instance.get("connectMode") != "PRIVATE_SERVICE_ACCESS"
            or instance.get("tier") != "BASIC"
            or instance.get("memorySizeGb") != 1
            or instance.get("redisVersion") != "REDIS_7_2"
            or instance.get("authEnabled") is not True
            or instance.get("transitEncryptionMode") != "SERVER_AUTHENTICATION"
        ):
            raise DataProvisionError("Redis의 소유권·네트워크·AUTH·TLS 설정이 다릅니다.")
        if instance.get("state") not in {"READY", "CREATING"}:
            raise DataProvisionError("Redis를 사용할 수 없습니다. 인스턴스 상태를 확인하세요.")
        if cp.get("create_time") and instance.get("createTime") != cp["create_time"]:
            raise DataProvisionError("기존 Redis가 다른 인스턴스로 교체됐습니다.")
        if instance.get("state") != "READY":
            return False
        self._check_endpoint(instance)
        return True

    @staticmethod
    def _check_endpoint(instance):
        try:
            host = ipaddress.ip_address(instance.get("host", ""))
        except ValueError:
            host = None
        certs = instance.get("serverCaCerts", [])
        if (
            not host
            or host.version != 4
            or not host.is_private
            or host.is_loopback
            or host.is_link_local
            or host.is_unspecified
            or instance.get("port") != 6378
            or not instance.get("createTime")
            or not certs
            or any(not ca.get("cert", "").startswith("-----BEGIN CERTIFICATE-----") for ca in certs)
        ):
            raise DataProvisionError("Redis 사설 주소·TLS CA 정보가 올바르지 않습니다.")

    def _publish(self, conn, cp, kind, instance):
        name = self.secret_names[kind]
        url = SECRETS_API + name
        secret = self.secrets.get(url)
        if secret is None:
            if cp.get(kind + "_version") or cp.get(kind + "_intent"):
                raise DataProvisionError("기존 Redis 연결 Secret이 사라졌습니다.")
            self.secrets.create(name, self.labels)
            return False
        if not labels_match(secret.get("labels", {}), self.labels):
            raise DataProvisionError("외부 Redis Secret은 덮어쓰지 않습니다. 소유권을 확인하세요.")
        if cp.get(kind + "_version"):
            if not self.secrets.version_enabled(url, cp[kind + "_version"]):
                raise DataProvisionError("Redis 연결 Secret 버전이 비활성화되어 있습니다.")
            return True
        if self._adopt_published_version(conn, cp, kind, url):
            return True
        if cp.get(kind + "_intent"):
            self._count_missing_version(conn, cp, kind)
            return False
        self._publish_new_version(conn, cp, kind, instance)
        return False

    def _adopt_published_version(self, conn, cp, kind, url):
        versions = self.api.call("GET", url + "/versions?pageSize=2")
        entries = versions.get("versions", [])
        if not entries and not versions.get("nextPageToken"):
            return False
        if not cp.get(kind + "_intent") or len(entries) != 1 or versions.get("nextPageToken"):
            raise ProviderConflict("Redis Secret has unregistered versions")
        version = entries[0]
        number = version.get("name", "").rsplit("/", 1)[-1]
        if version.get("state") != "ENABLED" or not VERSION_NUMBER.fullmatch(number):
            raise DataProvisionError("Redis 연결 Secret 버전을 확인할 수 없습니다.")
        cp[kind + "_version"] = number
        self._save(conn, cp)
        return True

    def _count_missing_version(self, conn, cp, kind):
        cp[kind + "_missing"] = cp.get(kind + "_missing", 0) + 1
        self._save(conn, cp)
        if cp[kind + "_missing"] >= MAX_MISSING_VERSION_OBSERVATIONS:
            raise ProviderConflict("Redis Secret publication has no version; do not republish")

    def _secret_value(self, kind, instance):
        if kind == SecretKind.CA:
            return "\n".join(ca["cert"] for ca in instance["serverCaCerts"])
        auth = self.api.call("GET", REDIS_API + self.instance + "/authString").get("authString")
        if not isinstance(auth, str) or not auth:
            raise DataProvisionError("Redis AUTH가 비어 있습니다.")
        query = urlencode(
            {
                "ssl_ca_certs": REDIS_CA_FILE,
                "ssl_cert_reqs": "required",
                "ssl_check_hostname": "false",
            }
        )
        return f"rediss://:{quote(auth, safe='')}@{instance['host']}:6378/0?{query}"

    def _publish_new_version(self, conn, cp, kind, instance):
        value = self._secret_value(kind, instance)
        cp[kind + "_intent"] = True
        self._save(conn, cp)
        try:
            result = self.secrets.add_version(SECRETS_API + self.secret_names[kind], value)
        except DataProvisionError:
            cp.pop(kind + "_intent")
            self._save(conn, cp)
            raise
        number = result.get("name", "").rsplit("/", 1)[-1]
        if not VERSION_NUMBER.fullmatch(number):
            raise DataProvisionError("Redis 연결 Secret 버전 번호가 올바르지 않습니다.")
        cp[kind + "_version"] = number
        self._save(conn, cp)
