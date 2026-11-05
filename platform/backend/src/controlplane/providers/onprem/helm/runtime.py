from typing import NamedTuple

import httpx
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError

from controlplane.domain.models import SecretBinding, Strategy
from controlplane.providers.contracts import ProviderConflict, ProviderUnavailable
from controlplane.providers.onprem.bindings import SecretRef
from controlplane.providers.onprem.connection import connect
from controlplane.providers.onprem.health_http import build_health_client, serves_release
from controlplane.providers.onprem.helm.client import HelmClient
from controlplane.providers.onprem.helm.kubeapi import READ_TIMEOUT, read_optional
from controlplane.providers.onprem.helm.models import HelmPlan
from controlplane.providers.onprem.helm.observation import RolloutState, observe, startup_issue
from controlplane.providers.onprem.helm.plan import app_plan, fingerprint, release_name
from controlplane.providers.onprem.observability import instrumentation_values
from controlplane.providers.onprem.provider import ArgoProvider

HELM_TEST_JOB_SUFFIX = "-helm-test"
TRAFFIC_SUFFIX = "-traffic"
FULL_WEIGHT = 100


class HelmTestResult(NamedTuple):
    passed: bool | None
    uid: str | None


class HelmRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.clients = None
        self.http = None
        self.app: HelmClient
        self.data: HelmClient

    def _client_for(self, artifact) -> HelmClient:
        s = self.settings
        return HelmClient(
            binary=s.binary,
            kubeconfig=s.kubeconfig,
            context=s.context,
            expected_server=s.server,
            artifact=artifact,
        )

    def connect(self):
        if self.clients:
            return
        s = self.settings
        self.clients = connect(kubeconfig=s.kubeconfig, context=s.context, expected_server=s.server)
        self.app = self._client_for(s.app_chart)
        self.data = self._client_for(s.data_chart)
        self.app.load_package()
        self.data.load_package()
        self.app.verify_version()
        self.http = build_health_client(timeout=5)

    def close(self):
        if self.clients:
            self.clients.close()
        if self.http:
            self.http.close()

    def bound(self, request, values):
        env = self.settings.environment.model_copy(deep=True)
        request = request.model_copy(deep=True)
        if values:
            for kind, selected, key in [
                ("db", request.resources.database == "postgresql", "DATABASE_URL"),
                ("redis", request.resources.redis, "REDIS_URL"),
            ]:
                if selected:
                    env.secret_refs[kind] = SecretRef(
                        name=values["credentialsSecret"],
                        key=key,
                        uid=values["credentialsSecretUid"],
                    )
                    request.secret_bindings.append(SecretBinding(name=key, binding=kind))
        request.resources.database_mode = "shared"
        return request, env

    def app_plan(self, request, key, values, published):
        bound, env = self.bound(request, values)
        bound.migration = None
        plan = app_plan(bound, env, key, self.settings.app_chart, published=published)
        telemetry = instrumentation_values(self.settings, request, key.value)
        if telemetry:
            plan.values["observability"] = telemetry
        if request.tracing_enabled is False:
            plan.values["env"]["OTEL_SDK_DISABLED"] = "true"
        plan.fingerprint = fingerprint(plan.model_dump(exclude={"fingerprint"}))
        return plan

    def _historical_client(self, chart_sha256: str) -> HelmClient:
        if chart_sha256 == self.settings.app_chart.sha256:
            return self.app
        artifacts = [
            self.settings.app_chart,
            *[b.app_chart for b in self.settings.previous_bindings],
        ]
        artifact = next((a for a in artifacts if a.sha256 == chart_sha256), None)
        if artifact is None:
            raise ProviderConflict("Historical chart is not approved for rollback")
        client = self._client_for(artifact)
        client.load_package()
        return client

    def verify_previous_binding(self, binding, plan):
        if binding != self.settings.fingerprint() and not any(
            b.fingerprint == binding and b.app_chart.sha256 == plan.chart_sha256
            for b in self.settings.previous_bindings
        ):
            raise ProviderConflict("Previous binding/chart pair was not approved")
        self._historical_client(plan.chart_sha256)

    def rollback_app(self, plan, receipt, *, expected_revision):
        return self._historical_client(plan.chart_sha256).rollback(
            plan, receipt, expected_revision=expected_revision
        )

    def migration(self, request, values):
        bound, env = self.bound(request, values)
        bound.strategy = Strategy.BLUE_GREEN
        return ArgoProvider(self.clients, env), bound

    def _read_rollout(self, plan: HelmPlan) -> dict:
        return self.clients.custom.get_namespaced_custom_object(
            "argoproj.io",
            "v1alpha1",
            plan.namespace,
            "rollouts",
            plan.release,
            _request_timeout=READ_TIMEOUT,
        )

    def _startup_issue(self, plan: HelmPlan, result: RolloutState, rollout: dict) -> str | None:
        try:
            return startup_issue(
                self.clients, plan, result.uid, rollout.get("status", {}).get("currentPodHash")
            )
        except (ApiException, HTTPError, ProviderUnavailable):
            return None

    def _apply_canary_traffic(self, plan: HelmPlan, result: RolloutState) -> None:
        traffic_name = plan.release + TRAFFIC_SUFFIX
        traffic = self.clients.custom.get_namespaced_custom_object(
            "traefik.io",
            "v1alpha1",
            plan.namespace,
            "traefikservices",
            traffic_name,
            _request_timeout=READ_TIMEOUT,
        )
        meta = traffic.get("metadata", {})
        annotations = meta.get("annotations", {})
        if (
            meta.get("name") != traffic_name
            or meta.get("namespace") != plan.namespace
            or meta.get("deletionTimestamp")
            or annotations.get("meta.helm.sh/release-name") != plan.release
            or annotations.get("meta.helm.sh/release-namespace") != plan.namespace
        ):
            raise ProviderConflict("Foreign canary traffic resource")
        services = traffic.get("spec", {}).get("weighted", {}).get("services", [])
        weights = {s.get("name"): s.get("weight") for s in services}
        stable_name, canary_name = plan.release + "-stable", plan.release + "-canary"
        stable, canary = weights.get(stable_name), weights.get(canary_name)
        if set(weights) != {stable_name, canary_name}:
            raise ProviderConflict("Foreign canary traffic targets")
        result.traffic_uid = meta["uid"]
        if len(services) == 2 and stable is None and canary is None:
            result.ready = False
            return
        if (
            len(services) != 2
            or type(stable) is not int
            or type(canary) is not int
            or stable < 0
            or canary < 0
            or stable + canary != FULL_WEIGHT
        ):
            raise ProviderConflict("Canary traffic weights are not reconciled")
        result.canary_weight = canary

    def observe(self, plan, uid=None):
        try:
            rollout = self._read_rollout(plan)
        except ApiException as exc:
            if exc.status == 404 and uid is None:
                return None
            raise
        result = observe(rollout, plan, expected_uid=uid)
        if result.observed and not result.ready:
            result.startup_issue = self._startup_issue(plan, result, rollout)
        if plan.strategy_type == "canary" and result.observed:
            self._apply_canary_traffic(plan, result)
        return result

    def verify_hostname(self):
        env = self.settings.environment
        own = release_name(env.application, env.environment_id)
        ingresses = self.clients.networking.list_namespaced_ingress(
            env.namespace, _request_timeout=READ_TIMEOUT
        ).items
        for ingress in ingresses:
            if (
                any(rule.host == env.hostname for rule in (ingress.spec.rules or []))
                and (ingress.metadata.annotations or {}).get("meta.helm.sh/release-name") != own
            ):
                raise ProviderConflict("Hostname is already owned by another deployment")
        try:
            obj = self.clients.custom.get_namespaced_custom_object(
                "argoproj.io",
                "v1alpha1",
                env.namespace,
                "rollouts",
                own,
                _request_timeout=READ_TIMEOUT,
            )
        except ApiException as exc:
            if exc.status == 404:
                return
            raise
        if obj.get("metadata", {}).get("annotations", {}).get("meta.helm.sh/release-name") != own:
            raise ProviderConflict("Existing Rollout is not owned by this Helm release")

    def probe(self, plan):
        try:
            response = self.http.get(
                "https://" + plan.values["route"]["hostname"] + plan.values["healthPath"]
            )
            return serves_release(response, plan.values["releaseId"])
        except httpx.HTTPError:
            return False

    def test_result(self, receipt, expected_uid=None, previous_uid=None) -> HelmTestResult:
        job = read_optional(
            self.clients.batch.read_namespaced_job,
            receipt.release + HELM_TEST_JOB_SUFFIX,
            "oneport-apps",
        )
        if job is None:
            return HelmTestResult(None, None)
        if previous_uid and job.metadata.uid == previous_uid:
            return HelmTestResult(None, None)
        if expected_uid and job.metadata.uid != expected_uid:
            raise ProviderConflict("Helm test Job UID changed")
        if (job.metadata.labels or {}).get("app.kubernetes.io/instance") != receipt.release:
            raise ProviderConflict("Foreign Helm test Job")
        if job.status.succeeded:
            return HelmTestResult(True, job.metadata.uid)
        if job.status.failed:
            return HelmTestResult(False, job.metadata.uid)
        return HelmTestResult(None, job.metadata.uid)
