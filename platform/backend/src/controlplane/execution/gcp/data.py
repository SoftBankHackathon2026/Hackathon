"""Durable, app-owned PostgreSQL preparation. No password is stored in the journal.

Only an unpublished, unique SQL user is ever created. Its URL is written to a
pinned Secret version before user creation, allowing recovery of lost responses
without reading credentials or resetting a user that an existing revision uses.
"""

import hashlib
import secrets

from controlplane.execution.gcp.api import ApiBacked, ApiProfile, GcpApiClient
from controlplane.execution.gcp.errors import DataProvisionError
from controlplane.execution.gcp.journal import (
    JournalTable,
    read_checkpoint,
    read_row,
    save_checkpoint,
)
from controlplane.execution.gcp.secret_manager import SECRETS_API, SecretManager, labels_match

__all__ = ["DataProvisionError", "GcpDataProvisioner", "data_status"]

POSTGRES_VERSION = "POSTGRES_17"
PRIVATE_NETWORK_PREFIX = "https://www.googleapis.com/compute/v1/"
UNUSABLE_STATES = {"FAILED", "SUSPENDED", "PENDING_DELETE"}
MAX_CREDENTIAL_ATTEMPTS = 8

API_PROFILE = ApiProfile(
    label="GCP data API",
    rejected="GCP DB 준비 API 요청 실패(HTTP {status}). 배포 계정 권한·API·할당량을 확인하세요.",
    failure=DataProvisionError,
)


def data_status(store, settings):
    return read_checkpoint(store, JournalTable.DATA, settings.environment.service_name)


class GcpDataProvisioner(ApiBacked):
    def __init__(self, session, settings):
        self.settings = settings
        self.env, self.policy = settings.environment, settings.data_provisioning
        self.api = GcpApiClient(session, API_PROFILE)
        self.secrets = SecretManager(self.api, self.env.project_id, self.env.region)
        self.instance = f"oneport-db-{self.policy.app_id}"
        self.secret = (
            f"projects/{self.env.project_id}/secrets/oneport_{self.policy.app_id}_database"
        )
        self.sql = f"https://sqladmin.googleapis.com/sql/v1beta4/projects/{self.env.project_id}"
        self.instance_url = f"{self.sql}/instances/{self.instance}"
        self.secret_url = SECRETS_API + self.secret
        self.owner = hashlib.sha256(
            f"{self.env.service_name}/{settings.resource_owner}".encode()
        ).hexdigest()[:32]
        self.labels = {"managed-by": "oneport", "oneport-owner": self.owner}
        self.fingerprint = hashlib.sha256(
            (self.owner + self.policy.fingerprint()).encode()
        ).hexdigest()

    def owned(self, labels):
        if not labels_match(labels, self.labels):
            raise DataProvisionError("같은 이름의 외부 DB·Secret이 있습니다. 소유권을 확인하세요.")

    def step(self, conn):
        cp = self._open_journal(conn)
        phase = cp.setdefault("phase", "instance")
        if self._operation_pending(conn, cp):
            return phase, None
        instance = self.api.call_unless_missing("GET", self.instance_url)
        if instance is None:
            self._create_instance(conn, cp, phase)
            return phase, None
        host = self._private_host(instance)
        if host is None:
            return phase, None
        if not self._advance(conn, cp, phase, host):
            return phase, None
        self._save(conn, cp)
        if cp["phase"] == "ready":
            return "ready", self._ready_binding(cp)
        return cp["phase"], None

    def _save(self, conn, cp):
        save_checkpoint(conn, JournalTable.DATA, self.env.service_name, cp)

    def _open_journal(self, conn):
        conn.execute(
            "INSERT INTO gcp_data(service_name,policy_fingerprint) VALUES(%s,%s) "
            "ON CONFLICT DO NOTHING",
            (self.env.service_name, self.fingerprint),
        )
        conn.commit()
        row = read_row(conn, JournalTable.DATA, self.env.service_name)
        if row["policy_fingerprint"] != self.fingerprint:
            raise DataProvisionError(
                "기존 DB의 앱·네트워크 설정이 변경됐습니다. 운영자 확인이 필요합니다."
            )
        return dict(row["checkpoint"])

    def _register_operation(self, conn, cp, result):
        if result:
            cp["operation"] = result["name"]
            self._save(conn, cp)

    def _operation_pending(self, conn, cp):
        if not cp.get("operation"):
            return False
        operation = self.api.call("GET", f"{self.sql}/operations/{cp['operation']}")
        if operation.get("status") != "DONE":
            return True
        cp.pop("operation")
        self._save(conn, cp)
        if operation.get("error"):
            raise DataProvisionError("Cloud SQL 작업이 실패했습니다. GCP 작업 이력을 확인하세요.")
        return False

    def _create_instance(self, conn, cp, phase):
        if phase != "instance":
            raise DataProvisionError("준비 중이거나 사용 중인 Cloud SQL 인스턴스가 사라졌습니다.")
        result = self.api.call_unless_conflict(
            "POST", f"{self.sql}/instances", body=self._instance_body()
        )
        self._register_operation(conn, cp, result)

    def _instance_body(self):
        return {
            "name": self.instance,
            "region": self.env.region,
            "databaseVersion": POSTGRES_VERSION,
            "settings": {
                "edition": "ENTERPRISE",
                "tier": "db-f1-micro",
                "availabilityType": "ZONAL",
                "dataDiskType": "PD_SSD",
                "dataDiskSizeGb": "10",
                "storageAutoResize": True,
                "deletionProtectionEnabled": True,
                "backupConfiguration": {"enabled": True, "startTime": "18:00"},
                "userLabels": self.labels,
                "ipConfiguration": {
                    "ipv4Enabled": False,
                    "privateNetwork": self.policy.network,
                    "allocatedIpRange": self.policy.allocated_ip_range,
                    "sslMode": "ENCRYPTED_ONLY",
                },
            },
        }

    def _private_host(self, instance):
        config = instance.get("settings", {})
        self.owned(config.get("userLabels", {}))
        network = config.get("ipConfiguration", {})
        if (
            instance.get("region") != self.env.region
            or instance.get("databaseVersion") != POSTGRES_VERSION
            or network.get("privateNetwork", "").removeprefix(PRIVATE_NETWORK_PREFIX)
            != self.policy.network
            or network.get("ipv4Enabled")
            or network.get("sslMode") != "ENCRYPTED_ONLY"
        ):
            raise DataProvisionError("Cloud SQL의 리전·엔진·사설 연결 설정이 등록 정보와 다릅니다.")
        if instance.get("state") in UNUSABLE_STATES:
            raise DataProvisionError("Cloud SQL을 사용할 수 없습니다. 인스턴스 상태를 확인하세요.")
        if instance.get("state") != "RUNNABLE":
            return None
        host = next(
            (a["ipAddress"] for a in instance.get("ipAddresses", []) if a["type"] == "PRIVATE"),
            None,
        )
        if not host:
            raise DataProvisionError(
                "Cloud SQL 사설 IP가 없습니다. Private Services Access를 확인하세요."
            )
        return host

    def _advance(self, conn, cp, phase, host):
        if phase == "instance":
            cp["phase"] = "database"
            return True
        if phase == "database":
            return self._ensure_database(conn, cp)
        if phase == "secret":
            return self._ensure_secret(cp)
        if phase == "credentials":
            return self._ensure_credentials(conn, cp, host)
        if phase != "ready":
            raise DataProvisionError("알 수 없는 DB 준비 단계입니다.")
        return True

    def _ensure_database(self, conn, cp):
        url = f"{self.instance_url}/databases"
        if self.api.call_unless_missing("GET", url + "/app") is None:
            result = self.api.call_unless_conflict("POST", url, body={"name": "app"})
            self._register_operation(conn, cp, result)
            return False
        cp["phase"] = "secret"
        return True

    def _ensure_secret(self, cp):
        secret = self.secrets.get(self.secret_url)
        if secret is None:
            self.secrets.create(self.secret, self.labels)
            return False
        self.owned(secret.get("labels", {}))
        cp["phase"] = "credentials"
        return True

    def _ensure_credentials(self, conn, cp, host):
        users = self.api.call("GET", f"{self.instance_url}/users").get("items", [])
        if cp.get("user") and any(u["name"] == cp["user"] for u in users):
            cp["phase"] = "ready"
            return True
        if cp.get("attempts", 0) >= MAX_CREDENTIAL_ATTEMPTS:
            raise DataProvisionError(
                "DB 계정 준비 재시도 한도를 넘었습니다. 운영자 확인이 필요합니다."
            )
        self.owned(self.api.call("GET", self.secret_url).get("labels", {}))
        user, password = "op_" + secrets.token_hex(12), secrets.token_urlsafe(32)
        url = f"postgresql://{user}:{password}@{host}:5432/app?sslmode=require"
        version = self.secrets.add_version(self.secret_url, url)["name"].rsplit("/", 1)[1]
        if not version.isdigit():
            raise DataProvisionError("Secret 버전 번호가 올바르지 않습니다.")
        cp.update(user=user, version=version, attempts=cp.get("attempts", 0) + 1)
        self._save(conn, cp)
        result = self.api.call(
            "POST",
            f"{self.instance_url}/users",
            body={"name": user, "password": password, "type": "BUILT_IN"},
        )
        self._register_operation(conn, cp, result)
        return False

    def _ready_binding(self, cp):
        self.owned(self.api.call("GET", self.secret_url).get("labels", {}))
        if not self.secrets.version_enabled(self.secret_url, cp["version"]):
            raise DataProvisionError("DB 연결 Secret 버전이 비활성화되어 있습니다.")
        return {
            "secret": self.secret,
            "version": cp["version"],
            "service_account": f"oneport-rt-{self.policy.app_id}@{self.env.project_id}.iam.gserviceaccount.com",
            "network": self.policy.network,
            "subnetwork": self.policy.subnetwork,
        }
