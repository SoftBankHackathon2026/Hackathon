from controlplane.domain.models import Runtime
from controlplane.providers.onprem import manifests
from controlplane.providers.onprem.adapter_base import OnpremAdapterBase, provider_step
from controlplane.providers.onprem.models import ArgoDeploymentRequest


class OnpremAdapter(OnpremAdapterBase):
    runtime = Runtime.ONPREM_ARGO

    def rollout_name(self) -> str:
        return manifests.name(self.settings.environment)

    def parse_request(self, payload):
        request = ArgoDeploymentRequest.model_validate(payload)
        self.settings.validate_request(request)
        return request

    def public_environment(self):
        env = self.settings.environment
        return {
            **self.environment_summary(),
            "name": f"{self.application} · 온프렘",
            "images": self.settings.allowed_images,
            "secret_bindings": sorted(env.secret_refs),
        }

    def deletion_info(self):
        return {
            "supported": True,
            "removes": [
                "공개 주소 Ingress",
                "Argo Rollout과 실행 중인 Pod",
                "active·preview Service",
            ],
            "data": [],
            "keeps": ["DB와 Secret (이 환경은 운영자가 준비한 것을 연결해서 씁니다)"],
            "recreatable": True,
            "after": "다시 배포하면 Service와 Rollout을 새로 만듭니다.",
        }

    def plan_steps(self, request):
        steps = [provider_step("validate", "등록 환경 확인", "이미지·자원·권한·Secret 참조")]
        if request.migration:
            steps.append(
                provider_step(
                    "migrate", "DB 마이그레이션", "호환되는 명령 성공 후 후보를 배포합니다."
                )
            )
        steps.extend(
            [
                provider_step("deploy", "후보 배포", "Argo Rollout 후보를 준비합니다."),
                provider_step("verify", "후보 검증", "비공개 후보의 앱 응답을 확인합니다."),
                provider_step(
                    "promote", "승격·운영 검증", "실제 응답 검증 후 Release 결과를 기록합니다."
                ),
            ]
        )
        return steps
