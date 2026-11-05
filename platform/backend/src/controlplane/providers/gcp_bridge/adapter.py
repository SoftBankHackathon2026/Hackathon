from controlplane.domain.models import Runtime, Step
from controlplane.execution.gcp_bridge.metadata import PHASES, public_metadata
from controlplane.providers.capabilities import FeatureSupport, provider_capabilities
from controlplane.providers.durable import DurableDeploymentProvider
from controlplane.providers.gcp_bridge.models import GcpRequest

PUBLIC_STATUS_FIELDS = frozenset(
    {
        "project_id",
        "source_commit",
        "image_uri",
        "digest",
        "revision",
        "previous_revision",
        "candidate_revision",
        "active_revision",
        "health",
        "rollback_status",
    }
)


class GcpAdapter(DurableDeploymentProvider):
    runtime = Runtime.GCP_CLOUD_RUN
    service_access = "authenticated"

    def __init__(self, settings, *, store=None):
        super().__init__(settings, store=store)
        self.application = settings.application
        self.environment_id = settings.environment_id
        self.service_access = settings.service_access

    def parse_request(self, payload):
        request = GcpRequest.model_validate(payload)
        self.settings.validate_request(request)
        return request

    def fingerprint(self):
        return self.settings.fingerprint()

    def capabilities(self):
        capabilities = provider_capabilities(self.runtime)
        return capabilities.model_copy(
            update={
                "configurable_fields": ["strategy"],
                "strategies": ["blue_green"],
                "features": capabilities.features
                | {
                    "blue_green": FeatureSupport(
                        stage="sdk_only", detail="0% tagged 후보 검증 → 100% 전환 → 운영 검증"
                    ),
                    "rollback": FeatureSupport(
                        stage="sdk_only",
                        detail="전환 후 실패 시 이전 explicit revision 복구·재검증",
                    ),
                },
            }
        )

    def snapshot(self, row):
        status = super().snapshot(row)
        public = public_metadata(row["checkpoint"].get("public", {}), self.settings)
        return status.model_copy(
            update={key: public[key] for key in PUBLIC_STATUS_FIELDS if key in public}
        )

    def public_environment(self):
        return {
            "id": self.environment_id,
            "name": "GCP · Cloud Run demo",
            "application": self.application,
            "runtime": self.runtime,
            "state": "registered",
            "executable": True,
            "connection_verified": False,
            "input_kind": "repository",
            "service_url": self.settings.service_url,
            "service_access": self.service_access,
            "request_template": {
                "application": self.application,
                "environment_id": self.environment_id,
                "source": {
                    "kind": "repository",
                    "repository": self.settings.allowed_repositories[0],
                },
                "strategy": "blue_green",
            },
            "limitations": [
                "기존 demo 서비스만 갱신. 루트 Dockerfile 필요. 저장소 기본 브랜치 HEAD를 빌드합니다.",
                "DB·Redis·환경변수·자원 설정 유지. Blue/Green 전환 후 실패 시 이전 revision 자동 복구.",
                "서비스 접근 정책은 운영자 설정입니다. 배포 provider는 IAM·공개 설정을 변경하지 않습니다.",
            ],
        }

    def plan_steps(self, request):
        return [
            Step(
                id="build",
                title="저장소 빌드",
                owner="provider",
                detail="기본 브랜치 HEAD의 루트 Dockerfile을 빌드합니다.",
            ),
            Step(
                id="deploy",
                title="기존 demo 갱신",
                owner="provider",
                detail="기존 환경·Secret·자원 설정을 유지합니다.",
            ),
            Step(
                id="verify",
                title="응답 확인",
                owner="provider",
                detail="리비전, / 및 /health 응답을 검증합니다.",
            ),
            *[
                Step(id=PHASES[stage][0], title=PHASES[stage][1], owner="provider", detail=detail)
                for stage, detail in (
                    ("PROMOTING", "검증된 후보에 production traffic 100% 전환"),
                    ("CHECKING", "전환 후 운영 URL 반복 검증. 실패 시 이전 revision 자동 복구"),
                )
            ],
        ]
