import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime

from controlplane.core.errors import InvalidRequest, OperationNotFound, RequestConflict
from controlplane.cost.models import CostInventory
from controlplane.domain.fleet import FleetConfig, MigrationConfig
from controlplane.repositories.data_mappings import MappingRepository
from controlplane.schemas.accounts import Identity
from controlplane.schemas.data_mapping import (
    DATA_KINDS,
    BindingView,
    DataBinding,
    DataMapping,
    DataMappingUpdate,
    DataResource,
    SavedMapping,
)

MIGRATION_NOT_FOUND = "등록된 마이그레이션 작업이 없습니다."
INVENTORY_MISSING = (
    "원본이나 대상 환경의 자원 목록을 아직 읽지 못했어요. 비용 수집이 끝나면 다시 확인하세요."
)
MAPPING_STALE = "확정한 뒤 자원 목록이 바뀌었어요. 데이터 자원 매핑을 다시 확인하세요."
HASH_CHANGED = "화면을 연 뒤 자원 목록이 바뀌었어요. 새로 고친 뒤 다시 확정하세요."
NEEDS_REVIEW = (
    "같은 종류의 데이터 자원이 여러 개예요. 시작 전에 데이터 자원 매핑을 확정하세요: {kinds}"
)
SOURCE_SET = "원본 자원마다 한 번씩 정해야 해요."
UNKNOWN_TARGET = "{source} 의 대상 {target} 은 같은 종류의 대상 자원이 아니에요."
TARGET_NOT_ALLOWED = "{source} 는 {action} 이라 대상을 고를 수 없어요."
REASON_REQUIRED = "{source} 를 옮기지 않으려면 이유를 적어야 해요."
STOP_TOKENS = frozenset(
    {
        "aws",
        "gcp",
        "onprem",
        "rds",
        "msk",
        "s3",
        "gcs",
        "cloud",
        "sql",
        "managed",
        "postgres",
        "postgresql",
        "kafka",
        "db",
        "database",
        "bucket",
        "storage",
        "oneaction",
        "review",
        "dev",
        "prod",
    }
)


KAFKA_MEMBER = re.compile(r"-(broker|controller|zookeeper)$")
INFRA_BUCKET_TOKENS = frozenset({"tfstate", "terraform", "log", "logs", "cloudtrail", "flowlogs"})


def infra_bucket(name: str) -> bool:
    return bool(set(re.split(r"[^a-z0-9]+", name.lower())) & INFRA_BUCKET_TOKENS)


def cluster_resources(env_id: str, inventory: CostInventory) -> list[DataResource]:
    groups: dict[tuple[str, str, str], list] = {}
    for workload in inventory.workloads:
        if workload.role not in DATA_KINDS:
            continue
        base = KAFKA_MEMBER.sub("", workload.name) if workload.role == "kafka" else workload.name
        groups.setdefault((workload.role, workload.namespace, base), []).append(workload)
    found = []
    for (kind, namespace, base), members in groups.items():
        pods = sum(m.replicas for m in members)
        parts = sorted(m.name for m in members)
        detail = f"파드 {pods}개"
        if len(members) > 1:
            detail += " (" + ", ".join(parts) + ")"
        found.append(
            DataResource(
                id=f"cluster:{namespace}/{base}",
                env_id=env_id,
                kind=kind,
                name=f"{namespace}/{base}",
                origin="cluster",
                detail=detail,
            )
        )
    return found


def data_resources(env_id: str, inventory: CostInventory) -> list[DataResource]:
    found = [
        DataResource(
            id=f"managed:{service.kind}:{service.name}",
            env_id=env_id,
            kind=service.kind,
            name=service.name,
            origin="managed",
            detail=service.size
            or (f"{service.storage_gib:g} GiB" if service.storage_gib else None),
        )
        for service in inventory.managed
        if service.kind in DATA_KINDS
        and not (service.kind == "object_storage" and infra_bucket(service.name))
    ]
    found += cluster_resources(env_id, inventory)
    return sorted(found, key=lambda r: (DATA_KINDS.index(r.kind), r.name))


def inventory_hash(sources: list[DataResource], targets: list[DataResource]) -> str:
    keys = sorted([f"s:{r.id}" for r in sources] + [f"t:{r.id}" for r in targets])
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:32]


def name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t not in STOP_TOKENS}


def similarity(left: str, right: str) -> float:
    a, b = name_tokens(left), name_tokens(right)
    return len(a & b) / len(a | b) if a | b else 0.0


def suggest(sources: list[DataResource], targets: list[DataResource]) -> list[BindingView]:
    views = []
    for kind in DATA_KINDS:
        same = [s for s in sources if s.kind == kind]
        options = [t for t in targets if t.kind == kind]
        candidates = [t.id for t in options]
        for source in same:
            if not options:
                views.append(
                    BindingView(source=source.id, kind=kind, action="create", basis="no_target")
                )
                continue
            if len(same) == 1 and len(options) == 1:
                views.append(
                    BindingView(
                        source=source.id,
                        kind=kind,
                        action="map",
                        target=options[0].id,
                        basis="only_pair",
                        candidates=candidates,
                    )
                )
                continue
            scored = sorted(
                ((similarity(source.name, t.name), t) for t in options),
                key=lambda pair: (-pair[0], pair[1].name),
            )
            best_score, best = scored[0]
            tied = len(scored) > 1 and scored[1][0] == best_score
            confident = best_score > 0 and not tied
            views.append(
                BindingView(
                    source=source.id,
                    kind=kind,
                    action="map",
                    target=best.id if confident else None,
                    basis="name" if confident else "none",
                    candidates=candidates,
                )
            )
    return views


def review_kinds(views: list[BindingView]) -> list[str]:
    return sorted({v.kind for v in views if v.basis in ("name", "none")}, key=DATA_KINDS.index)


def confirmed_views(
    saved: SavedMapping, sources: list[DataResource], targets: list[DataResource]
) -> list[BindingView]:
    kinds = {s.id: s.kind for s in sources}
    return [
        BindingView(
            **binding.model_dump(),
            kind=kinds[binding.source],
            basis="confirmed",
            candidates=[t.id for t in targets if t.kind == kinds[binding.source]],
        )
        for binding in saved.bindings
        if binding.source in kinds
    ]


def validate_bindings(
    bindings: list[DataBinding], sources: list[DataResource], targets: list[DataResource]
) -> list[DataBinding]:
    kinds = {s.id: s.kind for s in sources}
    given = [b.source for b in bindings]
    if sorted(given) != sorted(kinds):
        raise InvalidRequest(SOURCE_SET)
    target_kinds = {t.id: t.kind for t in targets}
    cleaned = []
    for binding in bindings:
        reason = (binding.reason or "").strip() or None
        if binding.action == "map":
            if target_kinds.get(binding.target or "") != kinds[binding.source]:
                raise InvalidRequest(
                    UNKNOWN_TARGET.format(source=binding.source, target=binding.target)
                )
        elif binding.target is not None:
            raise InvalidRequest(
                TARGET_NOT_ALLOWED.format(source=binding.source, action=binding.action)
            )
        if binding.action == "exclude" and reason is None:
            raise InvalidRequest(REASON_REQUIRED.format(source=binding.source))
        cleaned.append(binding.model_copy(update={"reason": reason}))
    return cleaned


class DataMappingService:
    def __init__(
        self,
        config: FleetConfig,
        inventory: Callable[[str], CostInventory | None],
        store: MappingRepository,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._inventory = inventory
        self._store = store
        self._now = now

    def _job(self, job_id: str) -> MigrationConfig:
        job = self._config.migration(job_id)
        if job is None:
            raise OperationNotFound(MIGRATION_NOT_FOUND)
        return job

    def _sides(self, job: MigrationConfig):
        source = self._inventory(job.source_env)
        target = self._inventory(job.target_env)
        sources = data_resources(job.source_env, source) if source else []
        targets = data_resources(job.target_env, target) if target else []
        return sources, targets, source is not None and target is not None

    def get(self, job_id: str) -> DataMapping:
        job = self._job(job_id)
        sources, targets, known = self._sides(job)
        digest = inventory_hash(sources, targets)
        base = {
            "job_id": job_id,
            "source_env": job.source_env,
            "target_env": job.target_env,
            "inventory_hash": digest,
            "sources": sources,
            "targets": targets,
        }
        suggested = suggest(sources, targets)
        if not known:
            return DataMapping(
                **base,
                status="unavailable",
                required=False,
                bindings=suggested,
                blockers=[INVENTORY_MISSING],
            )
        saved = self._store.get(job_id)
        if saved is not None and saved.inventory_hash == digest:
            return DataMapping(
                **base,
                status="ready",
                required=True,
                confirmed_by=saved.confirmed_by,
                confirmed_at=saved.confirmed_at,
                bindings=confirmed_views(saved, sources, targets),
            )
        if saved is not None:
            return DataMapping(
                **base,
                status="stale",
                required=True,
                confirmed_by=saved.confirmed_by,
                confirmed_at=saved.confirmed_at,
                bindings=suggested,
                blockers=[MAPPING_STALE],
            )
        kinds = review_kinds(suggested)
        if kinds:
            return DataMapping(
                **base,
                status="needs_review",
                required=True,
                bindings=suggested,
                blockers=[NEEDS_REVIEW.format(kinds=", ".join(kinds))],
            )
        return DataMapping(**base, status="ready", required=False, bindings=suggested)

    def update(
        self, job_id: str, body: DataMappingUpdate, identity: Identity | None
    ) -> DataMapping:
        job = self._job(job_id)
        sources, targets, known = self._sides(job)
        if not known:
            raise InvalidRequest(INVENTORY_MISSING)
        if body.inventory_hash != inventory_hash(sources, targets):
            raise RequestConflict(HASH_CHANGED)
        self._store.put(
            SavedMapping(
                job_id=job_id,
                inventory_hash=body.inventory_hash,
                bindings=validate_bindings(body.bindings, sources, targets),
                confirmed_by=identity.email if identity else None,
                confirmed_at=self._now(),
            )
        )
        return self.get(job_id)

    def blocker(self, job_id: str) -> str | None:
        mapping = self.get(job_id)
        if mapping.status in ("needs_review", "stale"):
            return mapping.blockers[0]
        return None
