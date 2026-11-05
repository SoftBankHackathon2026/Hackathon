"""Cloud Run transport double using the real protobuf request/response types."""

from google.api_core import exceptions as gexc
from google.cloud import run_v2

REVISION = run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
LATEST = run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST
READY = run_v2.Condition.State.CONDITION_SUCCEEDED


class CloudRunApi:
    def __init__(self, environment, image):
        self.writes = []
        self.fail_after_write = False
        self.delay_traffic = False
        self.env = environment
        self.service = run_v2.Service(
            name=environment.service_name,
            uid=environment.service_uid,
            etag="etag-1",
            generation=1,
            uri="https://demo-123.asia-northeast3.run.app",
            invoker_iam_disabled=True,
            ingress=run_v2.IngressTraffic.INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER,
            template=run_v2.RevisionTemplate(
                revision=environment.service + "-v1",
                service_account="runtime@test-project.iam.gserviceaccount.com",
                vpc_access=run_v2.VpcAccess(egress=run_v2.VpcAccess.VpcEgress.PRIVATE_RANGES_ONLY),
                containers=[
                    run_v2.Container(
                        image=image,
                        ports=[run_v2.ContainerPort(container_port=8080)],
                        resources=run_v2.ResourceRequirements(
                            limits={"cpu": "1", "memory": "1024Mi"}
                        ),
                        env=[
                            run_v2.EnvVar(name="EXISTING", value="preserve"),
                            run_v2.EnvVar(
                                name="DB_PASSWORD",
                                value_source=run_v2.EnvVarSource(
                                    secret_key_ref=run_v2.SecretKeySelector(
                                        secret="app-db", version="1"
                                    )
                                ),
                            ),
                        ],
                    )
                ],
            ),
            traffic=[run_v2.TrafficTarget(type_=LATEST, percent=100)],
        )
        self.revisions = {}
        self.save_revision()
        self.settle()

    def save_revision(self):
        template = self.service.template
        self.revisions[template.revision] = run_v2.Revision(
            name=self.env.service_name + "/revisions/" + template.revision,
            containers=template.containers,
            conditions=[run_v2.Condition(type_="Ready", state=READY)],
        )

    def settle(self):
        self.service.reconciling = False
        self.service.traffic_statuses = [
            run_v2.TrafficTargetStatus(
                type_=REVISION,
                revision=(self.service.template.revision if t.type_ == LATEST else t.revision),
                percent=t.percent,
                tag=t.tag,
                uri=f"https://{t.tag}---demo-123.asia-northeast3.run.app"
                if t.tag
                else self.service.uri,
            )
            for t in self.service.traffic
        ]

    def get_service(self, **kwargs):
        assert kwargs["name"] == self.env.service_name
        if getattr(self, "deleted", 0) > 1:
            raise gexc.NotFound("service deleted")
        if getattr(self, "deleted", 0):
            self.deleted += 1  # Deletion is a long-running operation: one more read.
        return run_v2.Service(self.service)

    def delete_service(self, *, name, **kwargs):
        assert name == self.env.service_name and kwargs["retry"] is None
        self.writes.append((["delete"], None))
        self.deleted = 1

    def get_revision(self, **kwargs):
        name = kwargs["name"].rsplit("/", 1)[-1]
        if name not in self.revisions:
            raise gexc.NotFound("missing")
        return run_v2.Revision(self.revisions[name])

    def update_service(self, *, service, update_mask, **kwargs):
        assert service.etag == self.service.etag
        assert service.name == self.service.name
        assert kwargs["retry"] is None
        fields = list(update_mask.paths)
        assert set(fields) <= {"template", "traffic"}
        self.writes.append((fields, run_v2.Service(service)))
        if "template" in fields:
            self.service.template = service.template
            self.save_revision()
        if "traffic" in fields:
            self.service.traffic = service.traffic
        self.service.generation += 1
        self.service.etag = f"etag-{self.service.generation}"
        self.service.reconciling = True
        if not self.delay_traffic:
            self.settle()
        if self.fail_after_write:
            self.fail_after_write = False
            raise gexc.DeadlineExceeded("Accepted, but response lost")
        return object()
