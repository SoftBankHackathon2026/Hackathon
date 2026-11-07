"""파악한 Inventory 를 서비스 대응표로 판정하고 사람 결정을 모은다.

결정적 표 조회다. 표에 맞는 행이 없거나 인용이 없으면 unclassified 로 두고
사람 결정으로 올린다 — 자동으로 진행하지 않는다(프로필 4절).

  report = classify.classify(inventory, "aws_to_gcp", schema_sql=ddl)
"""

import dataclasses

from crosscloud import model
from crosscloud.mapping import service_table, support_limits

# 대응이 없거나 판정할 수 없을 때 사람이 고르는 길(프로필 4절: 원본 유지·
# 기능 변경·이전 제외).
HUMAN_CHOICES = ("keep_source", "change_feature", "exclude_from_migration")

_SOURCE_CLOUD: dict[model.Direction, model.Cloud] = {
    "aws_to_gcp": "aws",
    "gcp_to_aws": "gcp",
}
_CITED_VERDICTS = frozenset({"equivalent", "substitute"})


@dataclasses.dataclass(frozen=True)
class Classification:
    """원본 자원·워크로드 하나의 판정.

    Attributes:
        source_id: 자원 native_id 또는 워크로드 name.
        verdict: 판정.
        row_id: 맞은 표 행. 맞은 행이 없으면 None.
    """

    source_id: str
    verdict: model.Verdict
    row_id: str | None


@dataclasses.dataclass(frozen=True)
class CompatibilityReport:
    """판정 결과 전체. decisions 가 비어 있어야 사람 개입 없이 진행한다."""

    direction: model.Direction
    classifications: tuple[Classification, ...]
    decisions: tuple[model.HumanDecision, ...]


def classify(
    inventory: model.Inventory,
    direction: model.Direction,
    *,
    schema_sql: str | None,
    table: tuple[service_table.ServiceRow, ...] = service_table.SERVICE_ROWS,
) -> CompatibilityReport:
    """Inventory 를 대응표로 판정한다.

    Args:
        inventory: 원본 클라우드에서 파악한 구성.
        direction: 이전 방향. 원본이 inventory.cloud 여야 한다.
        schema_sql: 앱 DB 의 DDL. 없으면 None 이고, 그러면 PostgreSQL
            인스턴스마다 지원 제한을 판정할 수 없다는 사람 결정이 생긴다.
        table: 서비스 대응표. 시험이 아니면 기본값을 쓴다.

    Returns:
        자원·워크로드별 판정과 사람 결정 목록.

    Raises:
        ValueError: direction 의 원본 클라우드가 inventory.cloud 와 다를 때.
    """
    if _SOURCE_CLOUD[direction] != inventory.cloud:
        raise ValueError(
            f"{direction} 의 원본은 {_SOURCE_CLOUD[direction]} 인데"
            f" inventory 는 {inventory.cloud} 에서 읽었다"
        )
    rows = tuple(row for row in table if row.direction == direction)
    classifications = tuple(
        _classify_resource(resource, rows) for resource in inventory.resources
    ) + tuple(
        _classify_workload(workload, rows) for workload in inventory.workloads
    )
    decisions = (
        _verdict_decisions(classifications)
        + _entry_decisions(inventory.workloads, rows)
        + _env_decisions(inventory.workloads)
        + _coupling_decisions(inventory)
        + _omission_decisions(inventory.omissions)
        + _support_limit_decisions(inventory.resources, schema_sql)
    )
    return CompatibilityReport(
        direction=direction,
        classifications=classifications,
        decisions=decisions,
    )


def _classify_resource(
    resource: model.Resource, rows: tuple[service_table.ServiceRow, ...]
) -> Classification:
    attributes = {kv.key: kv.value for kv in resource.attributes}
    for row in rows:
        if row.source_kind != resource.kind:
            continue
        if all(
            attributes.get(kv.key) == kv.value for kv in row.required_attributes
        ):
            return _classification(resource.native_id, row)
    return Classification(resource.native_id, "unclassified", None)


def _classify_workload(
    workload: model.Workload, rows: tuple[service_table.ServiceRow, ...]
) -> Classification:
    row = _workload_row(workload, rows)
    if row is None:
        return Classification(workload.name, "unclassified", None)
    return _classification(workload.name, row)


def _workload_row(
    workload: model.Workload, rows: tuple[service_table.ServiceRow, ...]
) -> service_table.ServiceRow | None:
    for row in rows:
        if row.source_kind == "workload" and (
            row.source_platform == workload.platform
        ):
            return row
    return None


def _classification(
    source_id: str, row: service_table.ServiceRow
) -> Classification:
    # 공식 문서 근거가 없는 대응은 대응으로 치지 않는다.
    if row.verdict in _CITED_VERDICTS and not row.citations:
        return Classification(source_id, "unclassified", None)
    return Classification(source_id, row.verdict, row.row_id)


def _verdict_decisions(
    classifications: tuple[Classification, ...],
) -> tuple[model.HumanDecision, ...]:
    return tuple(
        model.HumanDecision(
            subject=c.source_id,
            reason=c.verdict,
            signal=f"verdict={c.verdict}",
            options=HUMAN_CHOICES,
        )
        for c in classifications
        if c.verdict in ("unclassified", "no_equivalent")
    )


def _entry_decisions(
    workloads: tuple[model.Workload, ...],
    rows: tuple[service_table.ServiceRow, ...],
) -> tuple[model.HumanDecision, ...]:
    decisions = []
    for workload in workloads:
        row = _workload_row(workload, rows)
        if row is None:
            continue
        kinds = {entry.kind for entry in workload.entries}
        if kinds and kinds <= set(row.entry_kinds):
            continue
        decisions.append(
            model.HumanDecision(
                subject=workload.name,
                reason="non_http_entry",
                signal=f"entries={sorted(kinds) or 'none'}",
                options=HUMAN_CHOICES,
            )
        )
    return tuple(decisions)


def _env_decisions(
    workloads: tuple[model.Workload, ...],
) -> tuple[model.HumanDecision, ...]:
    decisions = []
    for workload in workloads:
        for entry in workload.env:
            subject = f"{workload.name}/{entry.key}"
            if entry.classification == "suspected_secret":
                decisions.append(
                    model.HumanDecision(
                        subject=subject,
                        reason="plaintext_secret_in_env",
                        signal="env 이름·값이 비밀로 보인다(값은 버렸다)",
                        options=("move_to_secret_store", "enter_in_target"),
                    )
                )
            elif entry.classification == "unknown":
                decisions.append(
                    model.HumanDecision(
                        subject=subject,
                        reason="env_value_unconfirmed",
                        signal="비밀인지 판단할 수 없다(값은 버렸다)",
                        options=("confirm_plain", "treat_as_secret"),
                    )
                )
    return tuple(decisions)


def _coupling_decisions(
    inventory: model.Inventory,
) -> tuple[model.HumanDecision, ...]:
    certainties: dict[str, set[str]] = {}
    for coupling in inventory.couplings:
        certainties.setdefault(coupling.workload, set()).add(coupling.certainty)
    return tuple(
        model.HumanDecision(
            subject=workload,
            reason="coupling_unconfirmed",
            signal="potential 결합만 있다",
            options=("confirm_coupling", "confirm_no_coupling"),
        )
        for workload, found in certainties.items()
        if found == {"potential"}
    )


def _omission_decisions(
    omissions: tuple[model.Omission, ...],
) -> tuple[model.HumanDecision, ...]:
    return tuple(
        model.HumanDecision(
            subject=omission.subject,
            reason="omission",
            signal=f"detected_by={omission.detected_by}",
            options=("acknowledge", "collect_again"),
        )
        for omission in omissions
        if omission.acknowledged_by is None
    )


def _support_limit_decisions(
    resources: tuple[model.Resource, ...], schema_sql: str | None
) -> tuple[model.HumanDecision, ...]:
    databases = [
        r.native_id for r in resources if r.kind == "postgres_instance"
    ]
    if not databases:
        return ()
    if schema_sql is None:
        signals = ["schema_not_provided"]
    else:
        signals = [
            f"{limit.kind}: {limit.subject}"
            for limit in support_limits.find_support_limits(schema_sql)
        ]
    return tuple(
        model.HumanDecision(
            subject=database,
            reason="transition_support_limit",
            signal=signal,
            options=HUMAN_CHOICES,
        )
        for database in databases
        for signal in signals
    )
