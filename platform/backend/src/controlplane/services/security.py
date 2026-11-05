from datetime import UTC, datetime

from controlplane.domain.fleet import EnvironmentSnapshot
from controlplane.domain.security_controls import (
    CONTROL_TARGETS,
    CONTROL_WHY,
    CONTROLS,
    STANDARDS,
    affected_components,
    evaluate,
)
from controlplane.integrations.base import FleetSource
from controlplane.schemas.security import (
    ControlResult,
    ControlView,
    EnvironmentPosture,
    Parity,
    SecurityPosture,
    SeverityCounts,
)

SEVERITY_WEIGHT = {"critical": 10, "high": 5, "medium": 3, "low": 1}


def posture_of(
    snapshot: EnvironmentSnapshot, results: dict[str, ControlResult]
) -> EnvironmentPosture:
    severity = SeverityCounts()
    earned = possible = 0
    passed = failed = unknown = 0
    for control in CONTROLS:
        status = results[control.id].status
        weight = SEVERITY_WEIGHT[control.severity]
        if status == "pass":
            passed += 1
            earned += weight
            possible += weight
        elif status == "fail":
            failed += 1
            possible += weight
            setattr(severity, control.severity, getattr(severity, control.severity) + 1)
        elif status == "unknown":
            unknown += 1
    return EnvironmentPosture(
        env_id=snapshot.config.id,
        name=snapshot.config.name,
        score=round(100 * earned / possible) if possible else 0,
        passed=passed,
        failed=failed,
        unknown=unknown,
        by_severity=severity,
    )


def parity_of(
    results: dict[str, dict[str, ControlResult]], source_env: str, target_env: str
) -> Parity:
    regressions, improvements = [], []
    for control in CONTROLS:
        before = results[source_env][control.id].status
        after = results[target_env][control.id].status
        if before == "pass" and after == "fail":
            regressions.append(control.id)
        elif before == "fail" and after == "pass":
            improvements.append(control.id)
    return Parity(
        source_env=source_env,
        target_env=target_env,
        regressions=regressions,
        improvements=improvements,
    )


class SecurityService:
    def __init__(self, source: FleetSource) -> None:
        self._source = source

    def _pairs(self, snapshots: list[EnvironmentSnapshot]) -> list[tuple[str, str]]:
        legacy = next(
            (s.config.id for s in snapshots if s.config.role == "legacy"), snapshots[0].config.id
        )
        targets = [s.config.id for s in snapshots if s.config.role == "target"]
        return [(legacy, target) for target in targets or [snapshots[-1].config.id]]

    def get_posture(self) -> SecurityPosture:
        snapshots = self._source.list_environments()
        results = {s.config.id: evaluate(s) for s in snapshots}
        for snapshot in snapshots:
            for control_id, result in results[snapshot.config.id].items():
                result.resources = affected_components(control_id, snapshot)
        parities = [parity_of(results, source, target) for source, target in self._pairs(snapshots)]
        return SecurityPosture(
            evaluated_at=datetime.now(UTC),
            source=self._source.mode,
            standards=STANDARDS,
            environments=[posture_of(s, results[s.config.id]) for s in snapshots],
            controls=[
                ControlView(
                    id=control.id,
                    title=control.title,
                    category=control.category,
                    severity=control.severity,
                    standard_refs=list(control.standard_refs),
                    remediation=control.remediation,
                    why=CONTROL_WHY.get(control.id, ""),
                    scope="resource" if control.id in CONTROL_TARGETS else "environment",
                    results={env: results[env][control.id] for env in results},
                )
                for control in CONTROLS
            ],
            parity=parities[0],
            parities=parities,
        )
