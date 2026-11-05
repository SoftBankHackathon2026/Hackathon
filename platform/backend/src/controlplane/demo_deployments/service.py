import httpx

from controlplane.builds.provider import BuildRejected, public_repository
from controlplane.core.errors import RequestConflict
from controlplane.demo_deployments.models import RUNTIMES, PreviewInput, StartRun
from controlplane.demo_deployments.planning import TargetPlanner
from controlplane.demo_deployments.snapshots import REQUIREMENTS, RunSnapshot
from controlplane.demo_deployments.source import DemoSource
from controlplane.demo_deployments.store import RunStore
from controlplane.providers.interface import DeploymentSpec

SOURCE_FIELDS = {"repository", "revision", "dockerfile", "context"}
RUN_FIELDS = (
    "id",
    "preview_id",
    "state",
    "build_id",
    "image",
    "error_code",
    "created_at",
    "updated_at",
)
TARGET_FIELDS = (
    "runtime",
    "application",
    "environment_id",
    "state",
    "attempt",
    "release_id",
    "error_code",
    "service_url",
    "attempts",
)


class RunService:
    def __init__(self, settings, build_settings, deployments, resolver=None):
        self.settings, self.build_settings, self.deployments = settings, build_settings, deployments
        self.store = RunStore(deployments.store.dsn) if deployments.store else None
        self.resolver = resolver or DemoSource(build_settings)
        self.planner = TargetPlanner(self)

    def destinations(self, application_id, identity=None):
        if not application_id:
            return dict(self.settings.targets)
        catalog = self.deployments.registry.catalog
        if not catalog:
            return {}
        try:
            catalog.application(application_id, identity)
        except RequestConflict:
            return {}
        found = {}
        for environment in catalog.list_environments(application_id):
            found.setdefault(environment["runtime"], environment["id"])
        return found

    def _resolve_commit(self, request: PreviewInput) -> tuple[str | None, list[str]]:
        try:
            return self.resolver.resolve(request.repository, request.revision), []
        except BuildRejected as exc:
            return None, [str(exc)]
        except httpx.HTTPError:
            return None, ["github_unavailable"]

    def preview(self, request: PreviewInput, identity=None):
        commit, blockers = self._resolve_commit(request)
        destinations = self.destinations(request.application_id, identity)
        snapshot = RunSnapshot(
            input=request.model_dump(mode="json"),
            source=request.model_dump(mode="json", include=SOURCE_FIELDS),
            commit=commit,
            blockers=blockers,
            targets=[
                self.planner.plan(runtime, destinations.get(runtime), request, identity)
                for runtime in RUNTIMES
            ],
            requirements=list(REQUIREMENTS),
        )
        row = self.store.preview(snapshot, identity)
        return snapshot.public(row["id"], row["expires_at"])

    def owner(self, environment_id):
        catalog = self.deployments.registry.catalog
        binding = catalog.binding(environment_id) if catalog else None
        if binding and binding["binding"] is not None:
            return binding["team"]
        return self.deployments.accounts.owners().get(environment_id)

    def validate(
        self, snapshot: RunSnapshot | dict, identity, *, runtimes=None, check_history=False
    ):
        snapshot = RunSnapshot.model_validate(snapshot)
        try:
            public_repository(snapshot.source["repository"])
        except BuildRejected:
            raise RequestConflict("올바른 공개 GitHub 저장소 주소를 입력하세요.") from None
        destinations = self.destinations(snapshot.application_id)
        for target in snapshot.targets:
            if runtimes is not None and target.runtime not in runtimes:
                continue
            spec = DeploymentSpec.model_validate(target.spec)
            self._check_binding(target, spec, destinations)
            self.deployments.plan(spec, identity)
            self._check_owner(target, spec.environment_id, identity)
            if check_history:
                self._check_history(target, spec.environment_id)

    def _check_binding(self, target, spec, destinations) -> None:
        adapter = self.deployments.registry.get(spec.environment_id)
        if (
            destinations.get(target.runtime) != spec.environment_id
            or not adapter
            or adapter.runtime != target.runtime
            or adapter.application != spec.application
            or adapter.fingerprint() != target.binding_fingerprint
        ):
            raise RequestConflict("시연 대상 설정이 변경됐습니다. 다시 확인하세요.")

    def _check_owner(self, target, environment_id, identity) -> None:
        owner = self.owner(environment_id)
        if (identity is None and owner != target.owner_team) or (
            identity and (target.owner_team != identity.team or owner not in (None, identity.team))
        ):
            raise RequestConflict("시연 대상 소유 팀이 변경됐습니다. 다시 확인하세요.")

    def _check_history(self, target, environment_id) -> None:
        with self.store.connect() as conn:
            latest = self.store.latest_release(conn, environment_id)
        if self.store.release_in_flight(latest) or (
            (latest["id"] if latest else None) != target.last_release_id
        ):
            raise RequestConflict("사전 확인 이후 배포 기록이 변경됐습니다. 다시 확인하세요.")

    def _validate_for_start(self, snapshot, identity):
        self.validate(snapshot, identity, check_history=True)

    def start(self, request, identity):
        return self.store.start(
            request, identity, self.settings.max_pending, self._validate_for_start
        )

    def launch(self, request, identity):
        payload = request.model_dump(mode="json", exclude={"idempotency_key"})
        with self.store.connect() as conn:
            old = self.store.run_by_key(conn, request.idempotency_key)
        if old:
            self.store.owned(old, identity)
            if old["snapshot"].get("input") != payload:
                raise RequestConflict("같은 요청 키에 다른 시연 설정을 사용할 수 없습니다.")
            return self.store.get(old["id"], identity)
        preview = self.preview(PreviewInput.model_validate(payload), identity)
        return self.store.start(
            StartRun(preview_id=preview["id"], idempotency_key=request.idempotency_key),
            identity,
            self.settings.max_pending,
            self._validate_for_start,
            direct_input=payload,
        )

    def retry(self, run_id, request, identity):
        def validate(snapshot, who):
            self.validate(snapshot, who, runtimes=request.runtimes)

        return self.store.retry(run_id, request, identity, validate, self.settings.max_pending)


def public_run(row):
    return {key: row[key] for key in RUN_FIELDS} | {
        "repository": row["snapshot"]["source"]["repository"],
        "commit": row["snapshot"]["commit"],
        "targets": [{key: target[key] for key in TARGET_FIELDS} for target in row["targets"]],
    }
