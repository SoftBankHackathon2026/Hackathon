"""Argo v1.10 SDK adapter. Never patch an Argo-managed Service selector.

Desired-state writes use UID/resourceVersion preconditions. A durable caller owns
intent, polling and private/public HTTP verification; API acceptance is not success.
"""

from copy import deepcopy

from controlplane.domain.models import DeploymentRequest
from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderUnavailable,
    ReleaseKey,
)
from controlplane.providers.onprem import manifests, pods
from controlplane.providers.onprem.models import ArgoObservation, Revision
from controlplane.providers.onprem.primitives import (
    TIMEOUT,
    KubernetesPrimitives,
    contains,
    read_or_none,
)

GROUP = {"group": "argoproj.io", "version": "v1alpha1", "plural": "rollouts"}
ROLLOUT_EXECUTION_POLICY_FIELDS = ("paused", "rollbackWindow", "workloadRef", "restartAt")
READY_REPLICA_FIELDS = ("replicas", "readyReplicas", "availableReplicas")


class ArgoProvider(KubernetesPrimitives):
    def verify(self, actual, desired):
        if actual and desired["kind"] == "Rollout":
            self._reject_foreign_rollout_policy(actual, desired)
        super().verify(actual, desired)

    @staticmethod
    def _reject_foreign_rollout_policy(actual: dict, desired: dict) -> None:
        spec = actual.get("spec", {})
        if any(spec.get(field) for field in ROLLOUT_EXECUTION_POLICY_FIELDS):
            raise ProviderConflict("Unexpected Rollout execution policy")
        strategy = spec.get("strategy", {})
        if set(strategy) != {"blueGreen"}:
            raise ProviderConflict("Only the registered BlueGreen strategy is allowed")
        if set(strategy["blueGreen"]) - set(desired["spec"]["strategy"]["blueGreen"]):
            raise ProviderConflict("Unregistered BlueGreen analysis or promotion policy")

    def _write_cas(self, method, **kwargs):
        try:
            return self.call(method, write=True, **kwargs)
        except ProviderConflict:
            raise ProviderUnavailable(
                "Argo resourceVersion changed; re-observe before writing"
            ) from None

    def read_rollout(self):
        return read_or_none(
            lambda: self.clients.custom.get_namespaced_custom_object(
                **GROUP,
                namespace=self.environment.namespace,
                name=manifests.name(self.environment),
                _request_timeout=TIMEOUT,
            ),
            "Argo",
        )

    def revision(self, obj):
        if not obj or not contains(
            obj.get("metadata", {}).get("labels", {}), pods.labels(self.environment)
        ):
            raise ProviderConflict("Missing or foreign Rollout")
        metadata = obj["metadata"]
        if metadata.get("deletionTimestamp"):
            raise ProviderConflict("Rollout is being deleted")
        try:
            release = obj["spec"]["template"]["metadata"]["labels"][pods.RELEASE]
            ReleaseKey(value=release)
            return Revision(
                uid=metadata["uid"],
                resource_version=metadata["resourceVersion"],
                generation=metadata["generation"],
                release=release,
            )
        except (KeyError, ValueError):
            raise ProviderConflict("Rollout identity is invalid") from None

    def _service(self, request, *, preview=False, create=False):
        desired = manifests.service(request, self.environment, preview=preview)
        actual = self.read_object("Service", desired["metadata"]["name"])
        if actual is None and create:
            actual = self.call(
                self.clients.core.create_namespaced_service,
                write=True,
                namespace=self.environment.namespace,
                body=desired,
            )
        if actual is None:
            raise ProviderConflict("Argo Service is missing")
        expected = deepcopy(desired)
        expected["spec"].pop("selector")
        selector = actual.get("spec", {}).get("selector", {})
        owner_labels = pods.labels(self.environment)
        if set(selector) != {*owner_labels, manifests.HASH} or not selector.get(manifests.HASH):
            raise ProviderConflict("Unrecognized Argo Service selector")
        if not contains(selector, owner_labels):
            raise ProviderConflict("Argo Service belongs to another app")
        self.verify(actual, expected)
        return actual

    def initialize(self, request):
        self.check_bindings(request)
        obj = self.read_rollout()
        if (
            obj is None
            and self.read_object("Ingress", manifests.name(self.environment)) is not None
        ):
            raise ProviderConflict("Public route exists without its Rollout")
        self._service(request, create=True)
        self._service(request, preview=True, create=True)
        return self.revision(obj) if obj else None

    def submit(self, request, release, *, expected: Revision | None):
        self.check_bindings(request)
        desired = manifests.rollout(request, self.environment, release)
        current = self.read_rollout()
        if current is None:
            if expected is not None:
                raise ProviderConflict("Rollout disappeared; refusing to recreate")
            obj = self.call(
                self.clients.custom.create_namespaced_custom_object,
                write=True,
                **GROUP,
                namespace=self.environment.namespace,
                body=desired,
            )
        else:
            revision = self.revision(current)
            if expected is not None and revision.uid != expected.uid:
                raise ProviderConflict("Rollout was replaced")
            if revision.release == release.value:
                self.verify(current, desired)
                return revision
            if expected is None or not revision.same_generation(expected):
                raise ProviderConflict("Rollout changed after durable preparation")
            obj = self._replace_rollout(current, desired)
        self.verify(obj, desired)
        return self.revision(obj)

    def _replace_rollout(self, current: dict, desired: dict) -> dict:
        body = deepcopy(current)
        body["spec"] = desired["spec"]
        body["metadata"].setdefault("annotations", {}).update(desired["metadata"]["annotations"])
        return self._write_cas(
            self.clients.custom.replace_namespaced_custom_object,
            **GROUP,
            namespace=self.environment.namespace,
            name=manifests.name(self.environment),
            body=body,
        )

    def _candidate_replica_sets(self, revision: Revision, candidate_hash) -> list[dict]:
        replica_sets = self.call(
            self.clients.apps.list_namespaced_replica_set,
            namespace=self.environment.namespace,
            label_selector=pods.label_selector(pods.labels(self.environment)),
        )
        return [
            rs
            for rs in replica_sets.get("items", [])
            if rs.get("metadata", {}).get("labels", {}).get(manifests.HASH) == candidate_hash
            and any(
                ref.get("uid") == revision.uid and ref.get("controller") is True
                for ref in rs.get("metadata", {}).get("ownerReferences", [])
            )
        ]

    @staticmethod
    def _replica_set_ready(
        rs: dict, desired_template: dict, request: DeploymentRequest, preview_matches: bool
    ) -> bool:
        template = deepcopy(rs.get("spec", {}).get("template", {}))
        template.get("metadata", {}).get("labels", {}).pop(manifests.HASH, None)
        status = rs.get("status", {})
        return (
            not rs.get("metadata", {}).get("deletionTimestamp")
            and contains(template, desired_template)
            and status.get("observedGeneration", 0) >= rs["metadata"].get("generation", 1)
            and all(status.get(k, 0) == request.replicas for k in READY_REPLICA_FIELDS)
            and preview_matches
        )

    def observe(self, request, release, *, expected_uid):
        obj = self.read_rollout()
        revision = self.revision(obj)
        if revision.uid != expected_uid:
            raise ProviderConflict("Rollout was replaced")
        desired = manifests.rollout(request, self.environment, release)
        self.verify(obj, desired)
        status = obj.get("status", {})
        try:
            observed = int(status.get("observedGeneration", 0)) == revision.generation
        except (ValueError, TypeError):
            observed = False
        candidate_hash = status.get("currentPodHash")
        preview = self._service(request, preview=True)
        active = self._service(request)
        candidates = self._candidate_replica_sets(revision, candidate_hash)
        ready = False
        if observed and candidate_hash and len(candidates) == 1:
            preview_hash = preview["spec"]["selector"][manifests.HASH]
            ready = self._replica_set_ready(
                candidates[0], desired["spec"]["template"], request, preview_hash == candidate_hash
            )
        reasons = {p.get("reason") for p in status.get("pauseConditions", [])}
        failed = observed and (status.get("phase") == "Degraded" or status.get("abort") is True)
        return ArgoObservation(
            revision=revision,
            ready=ready and not failed,
            active=bool(
                observed
                and candidate_hash
                and active["spec"]["selector"][manifests.HASH] == candidate_hash
                and status.get("blueGreen", {}).get("activeSelector") == candidate_hash
            ),
            paused=reasons == {"BlueGreenPause"},
            failed=failed,
        )

    def promote(self, request, release, *, expected: Revision):
        observed = self.observe(request, release, expected_uid=expected.uid)
        if not observed.revision.same_generation(expected) or not observed.ready:
            raise ProviderConflict("Candidate changed or is not ready")
        if observed.active:
            return
        if not observed.paused:
            raise ProviderConflict("Only a BlueGreenPause may be promoted")
        self._write_cas(
            self.clients.custom.patch_namespaced_custom_object_status,
            **GROUP,
            namespace=self.environment.namespace,
            name=manifests.name(self.environment),
            body={
                "metadata": {
                    "uid": expected.uid,
                    "resourceVersion": observed.revision.resource_version,
                },
                "status": {"pauseConditions": None},
            },
        )

    def publish(self):
        return self.ensure(manifests.ingress(self.environment))["metadata"]["uid"]

    def unpublish(self, *, expected_uid):
        obj = self.read_object("Ingress", manifests.name(self.environment))
        if obj is None:
            return
        self.verify(obj, manifests.ingress(self.environment))
        if obj["metadata"]["uid"] != expected_uid:
            raise ProviderConflict("Public Ingress was replaced")
        self.call(
            self.clients.networking.delete_namespaced_ingress,
            write=True,
            namespace=self.environment.namespace,
            name=manifests.name(self.environment),
            body={
                "preconditions": {
                    "uid": expected_uid,
                    "resourceVersion": obj["metadata"]["resourceVersion"],
                }
            },
        )
