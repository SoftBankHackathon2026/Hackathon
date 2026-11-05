from controlplane.domain.models import DeploymentRequest, Runtime, Strategy
from controlplane.providers.onprem.adapter_base import OnpremAdapterBase, provider_step
from controlplane.providers.onprem.helm.plan import release_name, strategy_values

STRATEGY_OPTIONS = [Strategy.BLUE_GREEN, Strategy.CANARY]
CHART_STRATEGIES = {"blueGreen": Strategy.BLUE_GREEN, "canary": Strategy.CANARY}
STRATEGY_LABELS = {Strategy.BLUE_GREEN: "블루그린", Strategy.CANARY: "카나리"}
ALL_INSTRUMENTATION = ["auto", "python", "java", "nodejs", "dotnet", "sdk"]

SAVED_DATA_SQL = """
SELECT checkpoint->'data_values' AS data FROM releases
WHERE runtime=%s AND application=%s AND environment_id=%s
  AND checkpoint->'data_values' IS NOT NULL AND checkpoint->'data_values' <> 'null'::jsonb
  AND archived IS DISTINCT FROM 'all'
ORDER BY created_at DESC LIMIT 1"""
SAVED_STRATEGY_SQL = """
SELECT checkpoint->'app_plan'->'values'->'strategy'->>'type' AS strategy FROM releases
WHERE runtime=%s AND application=%s AND environment_id=%s
  AND checkpoint->'app_plan' IS NOT NULL AND archived IS NULL
ORDER BY created_at DESC LIMIT 1"""
LAST_SUCCEEDED_REQUEST_SQL = """
SELECT request FROM releases
WHERE application=%s AND environment_id=%s AND state='succeeded'
ORDER BY created_at DESC LIMIT 1"""

LIMITATIONS = [
    "자동 블루그린·카나리. 전략 변경 시 기존 정상 버전으로 전환을 준비합니다.",
    "카나리는 구·신 버전을 함께 실행하며 단계별 검사 후 자동 전환합니다. 수동 승인은 미지원입니다.",
]


class HelmAdapter(OnpremAdapterBase):
    runtime = Runtime.ONPREM_HELM

    def rollout_name(self) -> str:
        return release_name(self.application, self.environment_id)

    @property
    def _switchable(self) -> bool:
        return self.settings.app_chart.supports_strategy_switch

    def saved_data(self):
        with self.store.connect() as conn:
            row = conn.execute(
                SAVED_DATA_SQL, (self.runtime, self.application, self.environment_id)
            ).fetchone()
        return row["data"] if row else None

    def saved_strategy(self) -> Strategy | None:
        with self.store.connect() as conn:
            row = conn.execute(
                SAVED_STRATEGY_SQL, (self.runtime, self.application, self.environment_id)
            ).fetchone()
        return CHART_STRATEGIES.get(row["strategy"]) if row else None

    def parse_request(self, payload):
        request = DeploymentRequest.model_validate(payload)
        self.settings.validate_request(request)
        strategy = self.saved_strategy()
        if strategy and request.strategy != strategy and not self._switchable:
            raise ValueError("Existing Helm strategy is immutable; use a new application")
        saved = self.saved_data()
        if saved and (
            saved["postgresql"]["enabled"] != (request.resources.database == "postgresql")
            or saved["redis"]["enabled"] != request.resources.redis
        ):
            raise ValueError("Existing data configuration cannot be changed during app deployment")
        return request

    def _request_template(self, strategy: Strategy | None, saved) -> DeploymentRequest:
        template = self.settings.demo_request()
        if self.settings.catalog_dynamic:
            with self.store.connect() as conn:
                previous = conn.execute(
                    LAST_SUCCEEDED_REQUEST_SQL, (self.application, self.environment_id)
                ).fetchone()
            if previous:
                template = DeploymentRequest.model_validate(previous["request"])
        if strategy:
            template.strategy = strategy
        if saved:
            template.resources.database = "postgresql" if saved["postgresql"]["enabled"] else "none"
            template.resources.redis = saved["redis"]["enabled"]
            if template.resources.database == "none":
                template.migration = None
        return template

    def _strategy_fields(self, strategy: Strategy | None) -> dict:
        locked = bool(strategy) and not self._switchable
        unavailable = {}
        if locked:
            current = STRATEGY_LABELS[strategy]
            message = (
                f"현재 앱은 {current}으로 배포되어 있습니다. "
                "현재 Helm 차트는 기존 앱의 전략 변경을 지원하지 않아 선택할 수 없습니다."
            )
            unavailable = {other: message for other in STRATEGY_OPTIONS if other != strategy}
        return {
            "helm_strategy_options": [strategy] if locked else list(STRATEGY_OPTIONS),
            "strategy_locked": locked,
            "helm_strategy_unavailable": unavailable,
        }

    def _instrumentation_options(self) -> list[str]:
        observability = self.settings.observability
        if not (
            observability
            and observability.python_image
            and self.settings.app_chart.supports_instrumentation
        ):
            return []
        return list(ALL_INSTRUMENTATION) if self._switchable else ["python"]

    def public_environment(self):
        strategy, saved = self.saved_strategy(), self.saved_data()
        template = self._request_template(strategy, saved)
        return {
            **self.environment_summary(),
            "name": "온프렘 · K3s / Helm",
            "engine": "helm",
            **self._strategy_fields(strategy),
            "instrumentation_options": self._instrumentation_options(),
            "images": [] if self.settings.catalog_dynamic else self.settings.allowed_images,
            "request_template": template.model_dump(mode="json"),
            "required_database": self.settings.required_database,
            "data_configured": saved is not None,
            "limitations": list(LIMITATIONS),
        }

    def deletion_info(self):
        saved = self.saved_data()
        data = []
        if saved and saved["postgresql"]["enabled"]:
            data.append("PostgreSQL StatefulSet과 데이터 볼륨(PVC)")
        if saved and saved["redis"]["enabled"]:
            data.append("Redis")
        if saved:
            data.append("DB·Redis 접속 정보 Secret")
        return {
            "supported": True,
            "removes": [
                "앱 Helm 릴리스 (Argo Rollout, Service, Ingress, Traefik 가중치)",
                "앱 테스트 Job",
            ],
            "data": data,
            "keeps": ["클러스터의 로그·trace 기록"],
            "recreatable": True,
            "data_required": self.settings.catalog_dynamic,
            "after": (
                "이 앱에서 온프렘 환경 연결도 해제합니다."
                if self.settings.catalog_dynamic
                else "데이터를 남기면 다시 배포할 때 같은 데이터를 다시 연결합니다."
            ),
        }

    def plan_steps(self, request):
        steps = []
        if request.resources.database != "none" or request.resources.redis:
            steps.append(
                provider_step("data", "데이터 준비", "전용 DB·PVC·Secret 재사용 및 인증 검사")
            )
        if request.migration:
            steps.append(
                provider_step("migration", "마이그레이션", "호환 가능한 명령을 Job으로 실행")
            )
        steps.append(provider_step("helm", "Helm 후보 설치", "패키지 SHA·revision·리소스 UID 기록"))
        steps.append(self._strategy_step(request))
        steps.append(
            provider_step("verify", "검증 및 공개", "Argo·후보 검사 후 공개 HTTPS·릴리즈 확인")
        )
        return steps

    def _strategy_step(self, request):
        if request.strategy != Strategy.CANARY:
            return provider_step(
                "blue_green", "블루그린 자동 전환", "후보 검사 후 새 버전으로 전체 트래픽 전환"
            )
        policy = strategy_values(request, self.settings.environment)["canary"]
        weights = " → ".join(f"{w}%" for w in policy["weights"])
        return provider_step(
            "canary",
            "카나리 자동 전환",
            f"재배포 시 {weights}. 각 단계 {policy['holdSeconds']}초 대기와 후보 검사. 첫 배포는 검사 후 공개.",
        )
