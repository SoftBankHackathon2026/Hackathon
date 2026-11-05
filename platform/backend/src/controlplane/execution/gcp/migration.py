"""Release-scoped DB migration as a Cloud Run Job. No credential is read or stored.

The job carries a deterministic per-app name and is replaced with the candidate
image, migration command and the pinned DATABASE_URL Secret before each run.
Execution-template labels identify this release, not Execution.generation (which
is local to each immutable execution). A persisted run intent is submitted once;
lost responses and restarts only read executions. Schema rollback is never run.
"""

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import urlencode

from psycopg.types.json import Jsonb

from controlplane.execution.gcp.api import ApiBacked, ApiProfile, GcpApiClient, JsonObject
from controlplane.execution.gcp.errors import MigrationError
from controlplane.providers.contracts import ProviderConflict

__all__ = ["GcpMigrationRunner", "MigrationError"]

RUN_API = "https://run.googleapis.com/v2"
MAX_EXECUTION_PAGES = 5
MAX_MISSING_EXECUTION_OBSERVATIONS = 12

API_PROFILE = ApiProfile(
    label="GCP migration API",
    rejected=(
        "GCP Migration Job API 요청 실패(HTTP {status}). "
        "배포 계정 권한·Cloud Run API·할당량을 확인하세요."
    ),
    failure=MigrationError,
)


@dataclass
class MigrationRun:
    conn: object
    row: dict
    cp: dict
    body: JsonObject

    @property
    def state(self) -> dict:
        return self.cp.setdefault("migration", {})

    @property
    def release(self) -> str:
        return self.row["id"]


class GcpMigrationRunner(ApiBacked):
    def __init__(self, session, settings):
        self.api = GcpApiClient(session, API_PROFILE)
        env, data = settings.environment, settings.data_provisioning
        self.job_id = f"oneport-migrate-{data.app_id}"
        self.base = f"{RUN_API}/projects/{env.project_id}/locations/{env.region}"
        self.job_url = f"{self.base}/jobs/{self.job_id}"
        self.owner = hashlib.sha256(
            f"{env.service_name}/{settings.resource_owner}".encode()
        ).hexdigest()[:32]
        self.labels = {"managed-by": "oneport", "oneport-owner": self.owner}

    def step(self, conn, row, cp, request, binding):
        run = MigrationRun(conn, row, cp, self._job_body(request, binding, row["id"]))
        if self._operation_pending(run):
            return "job", None
        job = self.api.call_unless_missing("GET", self.job_url)
        self._check_job_identity(run, job)
        executions = self._executions() if job else []
        current = [
            e for e in executions if e.get("labels", {}).get("oneport-release") == run.release
        ]
        if len(current) > 1:
            raise ProviderConflict("Multiple migration executions exist for this release")
        if current:
            return self._observe_execution(run, job, current[0])
        if run.state.get("run_requested"):
            return self._await_execution(run)
        if any(self._state(e) == "running" for e in executions):
            return "waiting", None
        if not job or not self._matches(job, run.body):
            return self._write_job(run, job)
        return self._start_run(run, job)

    def _save(self, run):
        run.conn.execute(
            "UPDATE releases SET checkpoint=%s, updated_at=now() WHERE id=%s",
            (Jsonb(run.cp), run.release),
        )
        run.conn.commit()
        run.row["checkpoint"] = run.cp

    def _job_body(self, request, binding, release):
        labels = {**self.labels, "oneport-release": release}
        return {
            "name": self.job_url.removeprefix(RUN_API + "/"),
            "labels": labels,
            "template": {
                "labels": labels,
                "taskCount": 1,
                "template": {
                    "containers": [
                        {
                            "image": request.source.image,
                            "command": request.migration.argv,
                            "args": [],
                            "env": [
                                {
                                    "name": "DATABASE_URL",
                                    "valueSource": {
                                        "secretKeyRef": {
                                            "secret": binding["secret"],
                                            "version": binding["version"],
                                        }
                                    },
                                }
                            ],
                            "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                        }
                    ],
                    "maxRetries": 0,
                    "timeout": f"{request.migration.timeout_seconds}s",
                    "serviceAccount": binding["service_account"],
                    "vpcAccess": {
                        "egress": "PRIVATE_RANGES_ONLY",
                        "networkInterfaces": [
                            {
                                "network": binding["network"],
                                "subnetwork": binding["subnetwork"],
                            }
                        ],
                    },
                    "executionEnvironment": "EXECUTION_ENVIRONMENT_GEN2",
                },
            },
        }

    def _operation_pending(self, run):
        m = run.state
        if not m.get("operation"):
            return False
        operation = self.api.call_unless_missing("GET", f"{RUN_API}/{m['operation']}")
        if operation is not None and not operation.get("done"):
            return True
        m.pop("operation")
        self._save(run)
        if operation and operation.get("error"):
            raise MigrationError(
                "Migration Job 설정 작업이 실패했습니다. GCP 작업 이력을 확인하세요."
            )
        return False

    def _check_job_identity(self, run, job):
        if job and any(job.get("labels", {}).get(k) != v for k, v in self.labels.items()):
            raise ProviderConflict("Migration Job is not owned by this registered service")
        job_uid = run.state.get("job_uid")
        if job_uid and (not job or job.get("uid") != job_uid):
            raise ProviderConflict("Migration Job identity changed")

    def _observe_execution(self, run, job, execution):
        m, body = run.state, run.body
        if any(execution.get("labels", {}).get(k) != v for k, v in self.labels.items()):
            raise ProviderConflict("Migration execution ownership changed")
        if not self._matches(job, body) or not self._execution_matches(
            execution.get("template"), body["template"]["template"]
        ):
            raise ProviderConflict("Migration Job or execution differs from the saved request")
        name = execution.get("name", "")
        if not name.startswith(body["name"] + "/executions/"):
            raise ProviderConflict("Unregistered migration execution identity")
        m["execution"] = name
        self._save(run)
        state = self._state(execution)
        if state == "failed":
            raise MigrationError(
                "DB Migration이 실패했습니다. 앱 배포는 시작하지 않았습니다. "
                "Migration Job 로그에서 원인을 확인하세요."
            )
        if state == "succeeded":
            return "done", {"job": self.job_id, "execution": name}
        return "waiting", None

    def _await_execution(self, run):
        m = run.state
        m["missing_observations"] = m.get("missing_observations", 0) + 1
        self._save(run)
        if m["missing_observations"] >= MAX_MISSING_EXECUTION_OBSERVATIONS:
            raise ProviderConflict("Migration run intent has no execution evidence; do not rerun")
        return "waiting", None

    def _write_job(self, run, job):
        m = run.state
        if m.get("configured_release") == run.release:
            raise ProviderConflict("Configured migration Job changed outside this release")
        if job:
            method, url = "PATCH", self.job_url
            write_body = {**run.body, "etag": job["etag"]}
        else:
            method, url = "POST", f"{self.base}/jobs?jobId={self.job_id}"
            write_body = {key: value for key, value in run.body.items() if key != "name"}
        result = self.api.call_unless_conflict(method, url, body=write_body)
        if result and result.get("name"):
            m["operation"] = result["name"]
            self._save(run)
        return "job", None

    def _start_run(self, run, job):
        m = run.state
        if job.get("reconciling"):
            return "job", None
        if job.get("terminalCondition", {}).get("state") == "CONDITION_FAILED":
            raise MigrationError("Migration Job이 준비되지 않았습니다. GCP 작업 이력을 확인하세요.")
        m["configured_release"] = run.release
        m["job_uid"] = job["uid"]
        m["run_requested"] = True
        self._save(run)
        self.api.call_unless_conflict("POST", f"{self.job_url}:run", body={"etag": job["etag"]})
        return "waiting", None

    @staticmethod
    def _state(execution):
        completed = next(
            (
                c.get("state")
                for c in execution.get("conditions", [])
                if c.get("type") == "Completed"
            ),
            None,
        )
        if completed == "CONDITION_FAILED" or any(
            int(execution.get(key) or 0) for key in ("failedCount", "cancelledCount")
        ):
            return "failed"
        if completed == "CONDITION_SUCCEEDED" and int(execution.get("succeededCount") or 0) == 1:
            return "succeeded"
        return "running"

    def _executions(self):
        executions, token = [], None
        for _ in range(MAX_EXECUTION_PAGES):
            query = {"pageSize": 100}
            if token:
                query["pageToken"] = token
            found = (
                self.api.call_unless_missing("GET", f"{self.job_url}/executions?{urlencode(query)}")
                or {}
            )
            executions.extend(found.get("executions", []))
            token = found.get("nextPageToken")
            if not token:
                return executions
        raise ProviderConflict("Migration execution history exceeds the safe observation limit")

    @staticmethod
    def _matches(job, desired):
        if isinstance(desired, dict):
            return isinstance(job, dict) and all(
                GcpMigrationRunner._matches(job.get(key), value) for key, value in desired.items()
            )
        if isinstance(desired, list):
            if not desired:
                return (job or []) == desired
            return (
                isinstance(job, list)
                and len(job) == len(desired)
                and all(
                    GcpMigrationRunner._matches(a, b) for a, b in zip(job, desired, strict=True)
                )
            )
        return job == desired or (job is None and desired in (0, False, ""))

    @staticmethod
    def _execution_matches(actual, desired):
        if not isinstance(actual, dict):
            return False
        expected = deepcopy(desired)
        if "vpcAccess" not in actual:
            expected.pop("vpcAccess", None)
        containers = actual.get("containers", [])
        for observed, requested in zip(containers, expected["containers"]):
            image = requested["image"]
            if image.startswith("docker.io/") and observed.get("image") == image.replace(
                "docker.io/", "mirror.gcr.io/", 1
            ):
                requested["image"] = observed["image"]
        return GcpMigrationRunner._matches(actual, expected)
