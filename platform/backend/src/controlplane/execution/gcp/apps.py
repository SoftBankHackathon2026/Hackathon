"""Durable setup of a catalog app; existing services are never implicitly adopted."""

import json

from google.api_core import exceptions as gexc
from google.cloud import run_v2
from psycopg.types.json import Jsonb

from controlplane.execution.gcp.api import ApiBacked, ApiProfile, GcpApiClient
from controlplane.execution.gcp.errors import DataProvisionError
from controlplane.execution.gcp.resource_names import WORKFLOW_EXECUTIONS_API, execution_name
from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderUnavailable,
    SubmissionUncertain,
)
from controlplane.providers.gcp.traffic import digest, normalized, revision_target, traffic

API_PROFILE = ApiProfile(
    label="App setup workflow",
    rejected="앱 실행 계정 준비 실패(HTTP {status}). Workflow 설정·권한을 확인하세요.",
    failure=DataProvisionError,
    timeout=10,
    invalid="Invalid app setup response",
)


def template_contains(actual, wanted):
    if isinstance(wanted, dict):
        return isinstance(actual, dict) and all(
            k in actual and template_contains(actual[k], v) for k, v in wanted.items()
        )
    if isinstance(wanted, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(wanted)
            and all(template_contains(a, b) for a, b in zip(actual, wanted))
        )
    return actual == wanted


def template_dict(template):
    return run_v2.RevisionTemplate.to_dict(template, always_print_fields_with_no_presence=False)


class GcpAppProvisioner(ApiBacked):
    def __init__(self, session, settings, provider):
        self.settings, self.provider = settings, provider
        self.api = GcpApiClient(session, API_PROFILE)
        self.env = settings.environment
        self.parent = f"projects/{self.env.project_id}/locations/{self.env.region}"
        self.executions = self.parent + "/workflows/oneport-app-provisioning/executions"
        self.labels = {"managed-by": "oneport", "oneport-owner": settings.catalog_owner}
        self.account = (
            f"oneport-rt-{self.env.application}@{self.env.project_id}.iam.gserviceaccount.com"
        )
        self.cp = {}

    def load(self, conn):
        conn.execute(
            "INSERT INTO gcp_apps(service_name,owner) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (self.env.service_name, self.settings.catalog_owner),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM gcp_apps WHERE service_name=%s", (self.env.service_name,)
        ).fetchone()
        if row["owner"] != self.settings.catalog_owner:
            raise ProviderConflict("Cloud Run app owner changed")
        self.cp = dict(row["checkpoint"])
        for key in ("service_uid", "service_url"):
            if self.cp.get(key):
                setattr(self.env, key, self.cp[key])

    def save(self, conn):
        conn.execute(
            "UPDATE gcp_apps SET checkpoint=%s,updated_at=now() WHERE service_name=%s AND owner=%s",
            (Jsonb(self.cp), self.env.service_name, self.settings.catalog_owner),
        )
        conn.commit()

    def prepare(self, conn):
        if self.cp.get("account_ready"):
            return True
        if not self.cp.get("execution"):
            self._start_workflow(conn)
            return False
        result = self.api.call("GET", WORKFLOW_EXECUTIONS_API + self.cp["execution"])
        if result.get("state") in {"ACTIVE", "QUEUED"}:
            return False
        if result.get("state") != "SUCCEEDED":
            raise DataProvisionError(
                "앱 실행 계정 Workflow가 실패했습니다. 실행 이력을 확인하세요."
            )
        self._verify_account(result)
        self.cp["account_ready"] = True
        self.save(conn)
        return True

    def _start_workflow(self, conn):
        result = self.api.call(
            "POST",
            WORKFLOW_EXECUTIONS_API + self.executions,
            body={
                "argument": json.dumps({"app_id": self.env.application}),
                "callLogLevel": "LOG_NONE",
            },
        )
        self.cp["execution"] = execution_name(
            self.env.project_id,
            self.executions,
            result.get("name", ""),
            "앱 Workflow 실행 이름이 등록된 환경과 다릅니다.",
        )
        self.save(conn)

    def _verify_account(self, result):
        try:
            data = json.loads(result["result"])
        except (KeyError, ValueError, TypeError):
            raise DataProvisionError("앱 실행 계정 응답 형식이 잘못됐습니다.") from None
        if (
            data.get("schema_version"),
            data.get("app_id"),
            data.get("runtime_service_account_email"),
        ) != (1, self.env.application, self.account):
            raise DataProvisionError("앱 실행 계정이 등록 정보와 다릅니다.")

    def read(self, conn):
        try:
            service = self.provider.services.get_service(
                name=self.env.service_name, retry=None, timeout=10
            )
        except gexc.NotFound:
            if self.env.service_uid:
                raise ProviderConflict("Pinned Cloud Run service disappeared") from None
            return None
        except gexc.GoogleAPICallError:
            raise ProviderUnavailable("Cloud Run app read failed") from None
        if any(service.labels.get(k) != v for k, v in self.labels.items()):
            raise ProviderConflict("Existing Cloud Run service belongs to another app")
        if self.env.service_uid:
            if service.uid != self.env.service_uid:
                raise ProviderConflict("Pinned Cloud Run service UID changed")
        elif not self.cp.get("create_intent"):
            raise ProviderConflict("Cloud Run service exists without a saved creation intent")
        elif service.uid:
            self.cp["service_uid"] = self.env.service_uid = service.uid
            self.save(conn)
        if service.uri:
            self.env.model_validate(self.env.model_dump() | {"service_url": service.uri})
            self.cp["service_url"] = self.env.service_url = service.uri
            self.save(conn)
        return service

    def empty_service(self):
        return run_v2.Service(
            template=run_v2.RevisionTemplate(
                service_account=self.account,
                containers=[run_v2.Container(name="app")],
            )
        )

    def template(self, request, release):
        return self.provider.candidate_template(
            self.empty_service(), request, self.env.service + "-op-" + release[:12], release
        )

    def snapshot(self, conn, request, release):
        service = self.read(conn)
        template = self.template(request, release)
        return {
            "first_deployment": True,
            "candidate": template.revision,
            "initial_template": run_v2.RevisionTemplate.to_dict(template),
            "before_template": digest(service.template) if service else None,
            "original_traffic": traffic(service) if service else [],
            "generation": service.generation if service else None,
        }

    @staticmethod
    def desired_traffic(cp):
        return [revision_target(cp["candidate"], 100)]

    def observe(self, conn, cp):
        service = self.read(conn)
        if service is None:
            return None
        expected = run_v2.RevisionTemplate(cp["initial_template"])
        actual = service.template
        if not template_contains(template_dict(actual), template_dict(expected)):
            raise ProviderConflict("Initial Cloud Run template differs from saved intent")
        self._check_pinned_fields(actual, expected)
        if normalized(traffic(service)) != normalized(self.desired_traffic(cp)):
            raise ProviderConflict("Initial Cloud Run traffic changed")
        if cp.get("candidate_template") and digest(actual) != cp["candidate_template"]:
            raise ProviderConflict("Initial Cloud Run template changed after observation")
        cp["candidate_template"] = digest(actual)
        return service

    @staticmethod
    def _check_pinned_fields(actual, expected):
        if (
            list(actual.volumes) != list(expected.volumes)
            or actual.vpc_access != expected.vpc_access
        ):
            raise ProviderConflict("Initial Cloud Run volumes/network changed")
        for a, b in zip(actual.containers, expected.containers):
            if list(a.env) != list(b.env) or list(a.volume_mounts) != list(b.volume_mounts):
                raise ProviderConflict("Initial Cloud Run app environment changed")

    def submit(self, conn, cp):
        service = self.read(conn)
        if service and service.template.revision == cp["candidate"]:
            self.observe(conn, cp)
            return
        template = run_v2.RevisionTemplate(cp["initial_template"])
        desired = self.desired_traffic(cp)
        if service:
            self._replace_service(service, cp, template, desired)
        else:
            self._create_service(conn, cp, template, desired)

    def _replace_service(self, service, cp, template, desired):
        if (
            service.generation != cp["generation"]
            or digest(service.template) != cp["before_template"]
            or normalized(traffic(service)) != normalized(cp["original_traffic"])
        ):
            raise ProviderConflict("Initial Cloud Run service changed before retry")
        self.provider.patch_service(
            run_v2.Service(
                name=service.name, etag=service.etag, template=template, traffic=desired
            ),
            ["template", "traffic"],
        )

    def _create_service(self, conn, cp, template, desired):
        self.cp["create_intent"] = cp["candidate"]
        self.save(conn)
        try:
            self.provider.services.create_service(
                parent=self.parent,
                service_id=self.env.service,
                retry=None,
                timeout=10,
                service=run_v2.Service(
                    template=template,
                    traffic=desired,
                    labels=self.labels,
                    ingress=run_v2.IngressTraffic.INGRESS_TRAFFIC_ALL,
                    invoker_iam_disabled=self.settings.service_access == "public",
                ),
            )
        except gexc.GoogleAPICallError:
            raise SubmissionUncertain("Cloud Run creation requires reconciliation") from None
