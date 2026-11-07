"""Durable Memorystore preparation through the operator's fixed cache workflow.

The journal contains resource names only. AUTH and CA data travel directly from
Redis to Secret Manager; an unknown workflow submission is never replayed.
"""

import hashlib
import ipaddress
import json
import re

from controlplane.execution.gcp.api import (
    ApiBacked,
    ApiProfile,
    GcpApiClient,
    RequestNotDelivered,
)
from controlplane.execution.gcp.errors import CacheSubmissionUnknown, DataProvisionError
from controlplane.execution.gcp.journal import (
    JournalTable,
    read_checkpoint,
    read_row,
    save_checkpoint,
)
from controlplane.execution.gcp.resource_names import (
    WORKFLOW_EXECUTIONS_API,
    execution_name,
    project_pattern,
)
from controlplane.execution.gcp.secret_manager import SECRETS_API, SecretManager

__all__ = ["CacheSubmissionUnknown", "GcpCacheProvisioner", "OWNER", "cache_status"]

OWNER = "oneport-cache-workflow"
REDIS_API = "https://redis.googleapis.com/v1/"

API_PROFILE = ApiProfile(
    label="GCP cache API",
    rejected="GCP 캐시 준비 API 요청 실패(HTTP {status}). 권한·Workflow 설정을 확인하세요.",
    failure=DataProvisionError,
)


def cache_status(store, settings):
    return read_checkpoint(store, JournalTable.CACHE, settings.environment.service_name)


class GcpCacheProvisioner(ApiBacked):
    def __init__(self, session, settings):
        self.settings = settings
        self.env, self.policy = settings.environment, settings.cache_provisioning
        self.api = GcpApiClient(session, API_PROFILE)
        self.secrets = SecretManager(self.api, self.env.project_id, self.env.region)
        self.parent = f"projects/{self.env.project_id}/locations/{self.env.region}"
        self.executions = self.parent + "/workflows/oneport-cache-provisioning/executions"
        self.instance = self.parent + "/instances/oneport-cache-" + self.policy.app_id
        self.secret = f"projects/{self.env.project_id}/secrets/oneport_{self.policy.app_id}_cache"
        self.secret_url = SECRETS_API + self.secret
        self.redis_url = REDIS_API + self.instance
        self.fingerprint = hashlib.sha256(
            (settings.resource_owner + self.policy.fingerprint()).encode()
        ).hexdigest()

    def binding(self, version="1"):
        return {
            "secret": self.secret,
            "version": version,
            "service_account": f"oneport-rt-{self.policy.app_id}@{self.env.project_id}.iam.gserviceaccount.com",
            "network": self.policy.network,
            "subnetwork": self.policy.subnetwork,
        }

    def step(self, conn):
        cp = self._open_journal(conn)
        phase = cp.get("phase", "new")
        if phase == "new":
            return self._submit(conn, cp)
        if phase == "submitting":
            raise CacheSubmissionUnknown(
                "캐시 생성 요청의 결과가 불확실합니다. Workflow 실행 이력을 확인하세요."
            )
        if phase == "creating":
            return self._observe_creation(conn, cp)
        if phase not in {"connection", "ready"}:
            raise DataProvisionError("알 수 없는 캐시 준비 단계입니다.")
        return self._connect(conn, cp, phase)

    def _save(self, conn, cp):
        save_checkpoint(conn, JournalTable.CACHE, self.env.service_name, cp)

    def _open_journal(self, conn):
        conn.execute(
            "INSERT INTO gcp_cache(service_name,instance_name,policy_fingerprint) VALUES(%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (self.env.service_name, self.instance, self.fingerprint),
        )
        conn.commit()
        row = read_row(conn, JournalTable.CACHE, self.env.service_name)
        if not row or row["policy_fingerprint"] != self.fingerprint:
            raise DataProvisionError(
                "캐시가 다른 서비스에 등록됐거나 앱·네트워크 설정이 변경됐습니다."
            )
        return dict(row["checkpoint"])

    def _execution(self, name):
        return execution_name(
            self.env.project_id,
            self.executions,
            name,
            "캐시 Workflow 실행 이름이 등록된 환경과 다릅니다.",
        )

    def _owned_secret(self):
        secret = self.secrets.get(self.secret_url)
        if secret is not None:
            labels = secret.get("labels", {})
            if labels.get("managed_by") != OWNER or labels.get("app_id") != self.policy.app_id:
                raise DataProvisionError(
                    "같은 이름의 외부 캐시 Secret이 있습니다. 소유권을 확인하세요."
                )
        return secret

    def _instance(self):
        instance = self.api.call("GET", self.redis_url)
        labels = instance.get("labels", {})
        if (
            instance.get("name") != self.instance
            or instance.get("state") != "READY"
            or labels.get("managed_by") != OWNER
            or labels.get("app_id") != self.policy.app_id
            or instance.get("authorizedNetwork") != self.policy.network
            or instance.get("connectMode") != "PRIVATE_SERVICE_ACCESS"
            or instance.get("authEnabled") is not True
            or instance.get("transitEncryptionMode") != "SERVER_AUTHENTICATION"
            or instance.get("tier") != "BASIC"
            or instance.get("memorySizeGb") != 1
        ):
            raise DataProvisionError("캐시의 소유권·사설 연결·보안 설정이 등록 정보와 다릅니다.")
        try:
            private = ipaddress.ip_address(instance.get("host", "")).is_private
        except ValueError:
            private = False
        certs = instance.get("serverCaCerts", [])
        if (
            not private
            or instance.get("port") != 6378
            or not certs
            or any(not isinstance(c.get("cert"), str) or not c["cert"] for c in certs)
        ):
            raise DataProvisionError("캐시 사설 주소·TLS 인증서가 올바르지 않습니다.")
        return instance

    def _workflow_argument(self):
        return {"action": "create", "app_id": self.policy.app_id}

    def _submit(self, conn, cp):
        cp["phase"] = "submitting"
        self._save(conn, cp)
        try:
            result = self.api.call(
                "POST",
                WORKFLOW_EXECUTIONS_API + self.executions,
                body={
                    "argument": json.dumps(self._workflow_argument()),
                    "callLogLevel": "LOG_NONE",
                    "executionHistoryLevel": "EXECUTION_HISTORY_BASIC",
                    "disableConcurrencyQuotaOverflowBuffering": True,
                },
            )
        except (DataProvisionError, RequestNotDelivered):
            cp["phase"] = "new"
            self._save(conn, cp)
            raise
        cp.update(execution=self._execution(result.get("name")), phase="creating")
        self._save(conn, cp)
        return "creating", None

    def _observe_creation(self, conn, cp):
        name = self._execution(cp["execution"])
        execution = self.api.call("GET", WORKFLOW_EXECUTIONS_API + name)
        if self._execution(execution.get("name")) != name or (
            _load_json(execution.get("argument", "null")) != self._workflow_argument()
        ):
            raise DataProvisionError("캐시 Workflow 실행이 요청한 앱과 다릅니다.")
        if execution.get("state") in {"ACTIVE", "QUEUED"}:
            return "creating", None
        if execution.get("state") != "SUCCEEDED":
            raise DataProvisionError("캐시 생성 Workflow가 실패했습니다. 실행 이력을 확인하세요.")
        expected = {
            "schema_version": 1,
            "action": "create",
            "app_id": self.policy.app_id,
            "instance": self.instance,
            "state": "READY",
        }
        if _load_json(execution.get("result", "null")) != expected:
            raise DataProvisionError("캐시 Workflow 결과가 요청과 다릅니다.")
        cp["phase"] = "connection"
        self._save(conn, cp)
        return "connection", None

    def _connect(self, conn, cp, phase):
        instance = self._instance()
        secret = self._owned_secret()
        if phase == "ready":
            return self._ready(cp, secret)
        if secret is None:
            self.secrets.create(self.secret, {"managed_by": OWNER, "app_id": self.policy.app_id})
            return phase, None
        auth = self.api.call("GET", self.redis_url + "/authString").get("authString")
        if not isinstance(auth, str) or not auth:
            raise DataProvisionError("Redis AUTH가 비어 있습니다.")
        value = {
            "schema_version": 1,
            "host": instance["host"],
            "port": instance["port"],
            "password": auth,
            "ca_pem": "\n".join(c["cert"] for c in instance["serverCaCerts"]),
        }
        result = self.secrets.add_version(self.secret_url, json.dumps(value))
        project = project_pattern(self.env.project_id)
        suffix = re.escape(self.secret.split("/", 2)[2])
        match = re.fullmatch(
            rf"projects/{project}/{suffix}/versions/([1-9][0-9]*)", result.get("name", "")
        )
        if not match:
            raise DataProvisionError("캐시 Secret 버전이 요청한 앱과 다릅니다.")
        cp.update(phase="ready", version=match[1])
        self._save(conn, cp)
        return "ready", self.binding(cp["version"])

    def _ready(self, cp, secret):
        if secret is None:
            raise DataProvisionError("앱에서 사용하는 캐시 Secret이 사라졌습니다.")
        if not self.secrets.version_enabled(self.secret_url, cp["version"]):
            raise DataProvisionError("캐시 연결 Secret 버전이 비활성화되어 있습니다.")
        return "ready", self.binding(cp["version"])


def _load_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
