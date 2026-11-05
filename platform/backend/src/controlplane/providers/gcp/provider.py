"""Cloud Run mutations guarded by registered UID, template and traffic snapshots.

Only template/traffic are patched. An optional prepared data binding adds a
pinned Secret and Direct VPC egress. A timed-out write is reconciled by reading first.
"""

from enum import StrEnum

from google.api_core import exceptions as gexc
from google.cloud import run_v2
from google.protobuf.field_mask_pb2 import FieldMask

from controlplane.providers.contracts import (
    ProviderConflict,
    ProviderUnavailable,
    SubmissionUncertain,
)
from controlplane.providers.gcp import tracing
from controlplane.providers.gcp.constants import REDIS_CA_DIR, REDIS_CA_VOLUME
from controlplane.providers.gcp.traffic import (
    CANDIDATE_TAG,
    digest,
    normalized,
    pinned_rollback_targets,
    targets,
    traffic,
    weights,
)

__all__ = [
    "CloudRunProvider",
    "RevisionState",
    "digest",
    "normalized",
    "targets",
    "traffic",
    "weights",
]

RELEASE_ENV = "LAUNCHPAD_RELEASE_ID"


class RevisionState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


class CloudRunProvider:
    def __init__(self, services, revisions, environment, tracing_settings=None):
        self.services, self.revisions, self.environment = services, revisions, environment
        self.data_binding = None
        self.cache_binding = None
        self.redis_binding = None
        self.tracing_settings = tracing_settings

    def read(self):
        try:
            service = self.services.get_service(
                name=self.environment.service_name, retry=None, timeout=10
            )
        except gexc.GoogleAPICallError as exc:
            raise ProviderUnavailable("Cloud Run read failed") from exc
        if service.uid != self.environment.service_uid:
            raise ProviderConflict("Registered Cloud Run service UID changed")
        return service

    def revision(self, name):
        try:
            return self.revisions.get_revision(
                name=self.environment.service_name + "/revisions/" + name, retry=None, timeout=10
            )
        except gexc.NotFound:
            return None
        except gexc.GoogleAPICallError as exc:
            raise ProviderUnavailable("Cloud Run revision read failed") from exc

    @staticmethod
    def revision_state(revision) -> RevisionState:
        if revision is None:
            return RevisionState.PENDING
        ready = next((c for c in revision.conditions if c.type_ == "Ready"), None)
        if ready and ready.state == run_v2.Condition.State.CONDITION_SUCCEEDED:
            return RevisionState.READY
        if ready and ready.state == run_v2.Condition.State.CONDITION_FAILED:
            return RevisionState.FAILED
        return RevisionState.PENDING

    def candidate_template(self, service, request, candidate, release):
        template = run_v2.RevisionTemplate(service.template)
        tracing.prepare_app(template)
        template.revision = candidate
        container = template.containers[0]
        self._apply_container(container, request)
        self._apply_environment(container, request, release)
        for env_var, binding in (
            ("DATABASE_URL", self.data_binding),
            ("CACHE_CONFIG", self.cache_binding),
            ("REDIS_URL", self.redis_binding),
        ):
            if binding:
                self.apply_binding(template, binding, env_var)
        if self.redis_binding:
            self._apply_redis_ca(template, container, self.redis_binding)
        if template.scaling.min_instance_count > request.replicas:
            raise ProviderConflict("Requested max instances is below registered minimum")
        template.scaling.max_instance_count = request.replicas
        tracing.configure(template, request, self.environment, release, self.tracing_settings)
        return template

    @staticmethod
    def _apply_container(container, request):
        container.image = request.source.image
        container.resources.limits["cpu"] = request.cpu
        container.resources.limits["memory"] = f"{request.memory_mib}Mi"
        protocol = container.ports[0].name if container.ports else ""
        container.ports = [
            run_v2.ContainerPort(name=protocol or "http1", container_port=request.port)
        ]

    @staticmethod
    def _apply_environment(container, request, release):
        secret_names = {e.name for e in container.env if e.value_source.secret_key_ref.secret}
        if secret_names.intersection(request.env):
            raise ProviderConflict(
                "Plain environment values cannot replace existing Secret references"
            )
        updates = {**request.env, RELEASE_ENV: release}
        container.env = [e for e in container.env if e.name not in updates] + [
            run_v2.EnvVar(name=name, value=value) for name, value in sorted(updates.items())
        ]

    @staticmethod
    def _apply_redis_ca(template, container, binding):
        existing = next((v for v in template.volumes if v.name == REDIS_CA_VOLUME), None)
        if existing and existing.secret.secret != binding["ca_secret"]:
            raise ProviderConflict("An external Redis CA volume cannot be replaced")
        for index, app in enumerate(template.containers):
            for mount in app.volume_mounts:
                owns_name = mount.name == REDIS_CA_VOLUME
                owns_path = mount.mount_path == REDIS_CA_DIR
                if (owns_name or owns_path) and not (index == 0 and owns_name and owns_path):
                    raise ProviderConflict("Existing volume mount conflicts with Redis CA")
        template.volumes = [v for v in template.volumes if v.name != REDIS_CA_VOLUME] + [
            run_v2.Volume(
                name=REDIS_CA_VOLUME,
                secret=run_v2.SecretVolumeSource(
                    secret=binding["ca_secret"],
                    default_mode=0o444,
                    items=[run_v2.VersionToPath(path="ca.pem", version=binding["ca_version"])],
                ),
            )
        ]
        container.volume_mounts = [
            m for m in container.volume_mounts if m.name != REDIS_CA_VOLUME
        ] + [run_v2.VolumeMount(name=REDIS_CA_VOLUME, mount_path=REDIS_CA_DIR)]

    def validate_cache_target(self, binding):
        self.apply_binding(run_v2.RevisionTemplate(self.read().template), binding, "CACHE_CONFIG")

    def validate_redis_target(self, binding):
        self.apply_binding(run_v2.RevisionTemplate(self.read().template), binding, "REDIS_URL")

    @staticmethod
    def apply_binding(template, binding, env_var):
        container = template.containers[0]
        existing = next((e for e in container.env if e.name == env_var), None)
        if existing and existing.value_source.secret_key_ref.secret != binding["secret"]:
            raise ProviderConflict(f"An external {env_var} cannot be replaced")
        if template.service_account != binding["service_account"]:
            raise ProviderConflict("Registered runtime account does not match the data app")
        if template.vpc_access.connector:
            raise ProviderConflict("Existing VPC connector cannot be replaced")
        interfaces = template.vpc_access.network_interfaces
        if interfaces and (
            len(interfaces) != 1
            or interfaces[0].network != binding["network"]
            or interfaces[0].subnetwork != binding["subnetwork"]
        ):
            raise ProviderConflict("Existing VPC interface does not match the data policy")
        if not interfaces:
            template.vpc_access = run_v2.VpcAccess(
                egress=run_v2.VpcAccess.VpcEgress.PRIVATE_RANGES_ONLY,
                network_interfaces=[
                    run_v2.VpcAccess.NetworkInterface(
                        network=binding["network"], subnetwork=binding["subnetwork"]
                    )
                ],
            )
        container.env = [e for e in container.env if e.name != env_var] + [
            run_v2.EnvVar(
                name=env_var,
                value_source=run_v2.EnvVarSource(
                    secret_key_ref=run_v2.SecretKeySelector(
                        secret=binding["secret"], version=binding["version"]
                    )
                ),
            )
        ]

    def snapshot(self, request, release):
        service = self.read()
        current = weights(service)
        if service.reconciling or len(current) != 1 or list(current.values()) != [100]:
            raise ProviderConflict("An existing stable service with one 100% revision is required")
        if any(t.tag and not t.tag.startswith("oneport-") for t in service.traffic):
            raise ProviderConflict("Traffic tags are managed outside Oneport")
        stable = next(iter(current))
        revision = self.revision(stable)
        if self.revision_state(revision) != RevisionState.READY:
            raise ProviderConflict("Previous revision is not ready")
        stable_release = next(
            (e.value for e in revision.containers[0].env if e.name == RELEASE_ENV), None
        )
        candidate = self.environment.service + "-op-" + release[:12]
        return {
            "stable": stable,
            "candidate": candidate,
            "stable_release": stable_release,
            "original_traffic": traffic(service),
            "rollback_traffic": pinned_rollback_targets(service, stable),
            "before_template": digest(service.template),
            "candidate_template": digest(
                self.candidate_template(service, request, candidate, release)
            ),
            "generation": service.generation,
        }

    def patch_service(self, service, fields):
        try:
            self.services.update_service(
                service=service, update_mask=FieldMask(paths=fields), retry=None, timeout=10
            )
        except gexc.GoogleAPICallError as exc:
            raise SubmissionUncertain("Cloud Run update requires reconciliation") from exc

    def submit(self, request, release, cp):
        service = self.read()
        desired = targets(cp["stable"], cp["candidate"], 0)
        if digest(service.template) == cp["candidate_template"]:
            if normalized(traffic(service)) != normalized(desired):
                raise ProviderConflict("Candidate traffic changed outside this release")
            return
        if (
            service.generation != cp["generation"]
            or digest(service.template) != cp["before_template"]
            or normalized(traffic(service)) != normalized(cp["original_traffic"])
        ):
            raise ProviderConflict("Service changed after the release snapshot")
        template = self.candidate_template(service, request, cp["candidate"], release)
        if digest(template) != cp["candidate_template"]:
            raise ProviderConflict("Candidate template does not match the saved intent")
        self.patch_service(
            run_v2.Service(
                name=service.name, etag=service.etag, template=template, traffic=desired
            ),
            ["template", "traffic"],
        )

    def observe(self, cp, expected_traffic):
        service = self.read()
        if digest(service.template) != cp["candidate_template"]:
            raise ProviderConflict("Candidate template changed outside this release")
        if normalized(traffic(service)) != normalized(expected_traffic):
            raise ProviderConflict("Traffic changed outside this release")
        return service

    def switch(self, cp, before, desired):
        service = self.read()
        if digest(service.template) != cp["candidate_template"]:
            raise ProviderConflict("Candidate template changed outside this release")
        if normalized(traffic(service)) == normalized(desired):
            return
        if normalized(traffic(service)) != normalized(before):
            raise ProviderConflict("Traffic changed outside this release")
        self.patch_service(
            run_v2.Service(name=service.name, etag=service.etag, traffic=desired), ["traffic"]
        )

    @staticmethod
    def converged(service, desired_weights):
        return not service.reconciling and weights(service) == {
            k: v for k, v in desired_weights.items() if v
        }

    @staticmethod
    def candidate_url(service, cp):
        return next(
            (
                t.uri
                for t in service.traffic_statuses
                if t.tag == CANDIDATE_TAG and t.revision.rsplit("/", 1)[-1] == cp["candidate"]
            ),
            None,
        )
