from dataclasses import dataclass
from enum import StrEnum

from controlplane.domain.migration import FAILURE_STATUS, FLOW_STAGES, Stage


class PlanPhase(StrEnum):
    ENVIRONMENT = "environment"
    APPLICATION = "application"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class StepId(StrEnum):
    CONNECT = "connect"
    DATA_MAPPING = "data_mapping"
    INFRA_CHECK = "infra_check"
    IMAGES = "images"
    SECRETS = "secrets"
    IDENTITY = "identity"
    DEPLOY = "deploy"
    DATA_SYNC = "data_sync"
    TRAFFIC_SWITCH = "traffic_switch"
    VERIFY = "verify"


Route = tuple[str, str]

ONPREM = "onprem"
CLOUDS = ("aws", "gcp")
PROVIDER_LABELS = {"onprem": "온프렘", "aws": "AWS", "gcp": "GCP"}
UNSUPPORTED_REASON = "이 방향은 아직 지원하지 않아요."


@dataclass(frozen=True)
class StepDef:
    id: StepId
    phase: PlanPhase
    title: str
    description: str


@dataclass(frozen=True)
class PlannedStep:
    definition: StepDef
    tool: str | None
    status: StepStatus
    runnable: bool


@dataclass(frozen=True)
class Signals:
    source_connected: bool
    target_connected: bool
    mapping_status: str | None
    stage: Stage | None
    failed_stage: Stage | None
    verify_passed: bool | None


STEPS: tuple[StepDef, ...] = (
    StepDef(
        StepId.CONNECT,
        PlanPhase.ENVIRONMENT,
        "환경 연결",
        "원본과 대상 클러스터를 플랫폼이 읽을 수 있는지 확인",
    ),
    StepDef(
        StepId.DATA_MAPPING,
        PlanPhase.ENVIRONMENT,
        "데이터 자원 매핑",
        "원본 DB·Kafka·버킷을 대상 자원과 짝지어 확정",
    ),
    StepDef(
        StepId.INFRA_CHECK,
        PlanPhase.ENVIRONMENT,
        "대상 기반 점검",
        "대상의 레지스트리·비밀 저장소·인증·감사로그 기반 확인",
    ),
    StepDef(
        StepId.IMAGES,
        PlanPhase.APPLICATION,
        "이미지 이관",
        "원본 레지스트리 이미지를 대상 레지스트리로 복사·검증",
    ),
    StepDef(
        StepId.SECRETS,
        PlanPhase.APPLICATION,
        "비밀값 이관",
        "원본 비밀값을 대상 비밀 저장소로 옮기고 DB 계정 교체",
    ),
    StepDef(
        StepId.IDENTITY,
        PlanPhase.APPLICATION,
        "계정·권한 이관",
        "원본 사용자·그룹·권한을 대상 인증·권한 체계로 구성",
    ),
    StepDef(
        StepId.DEPLOY,
        PlanPhase.APPLICATION,
        "앱 배포",
        "이관한 이미지와 비밀값으로 대상 환경에 앱을 띄우고 준비 상태 확인",
    ),
    StepDef(
        StepId.DATA_SYNC,
        PlanPhase.APPLICATION,
        "데이터 동기화",
        "사전 점검, 초기 복사, 변경분 추적 뒤 전환 승인 대기",
    ),
    StepDef(
        StepId.TRAFFIC_SWITCH,
        PlanPhase.APPLICATION,
        "트래픽 전환",
        "원본 쓰기 차단, 최종 반영, 대상으로 쓰기 전환",
    ),
    StepDef(
        StepId.VERIFY,
        PlanPhase.APPLICATION,
        "검증",
        "전환 후 원본·대상 데이터와 앱 상태 확인",
    ),
)

AWS_TO_ONPREM: Route = ("aws", ONPREM)
CLOUD_PAIRS: tuple[Route, ...] = (("aws", "gcp"), ("gcp", "aws"))
ONPREM_CLOUD: tuple[Route, ...] = tuple(
    pair for cloud in CLOUDS for pair in ((ONPREM, cloud), (cloud, ONPREM))
)

TOOLS: dict[StepId, dict[Route, str]] = {
    StepId.IMAGES: {AWS_TO_ONPREM: "migration.image_migration"},
    StepId.SECRETS: {
        AWS_TO_ONPREM: "migration.secrets",
        **{pair: "crosscloud" for pair in CLOUD_PAIRS},
    },
    StepId.IDENTITY: {AWS_TO_ONPREM: "migration.identity"},
    StepId.DATA_SYNC: {
        **{pair: "migration.cutover" for pair in ONPREM_CLOUD},
        **{pair: "migration.cloud" for pair in CLOUD_PAIRS},
    },
    StepId.TRAFFIC_SWITCH: {
        **{pair: "migration.cutover · durable-ingress" for pair in ONPREM_CLOUD},
        **{pair: "migration.cloud" for pair in CLOUD_PAIRS},
    },
    StepId.VERIFY: {
        **{pair: "migration.verify" for pair in ONPREM_CLOUD},
        **{pair: "migration.cloud" for pair in CLOUD_PAIRS},
    },
}

CUTOVER_STEPS = (StepId.DATA_SYNC, StepId.TRAFFIC_SWITCH, StepId.VERIFY)
PLATFORM_STEPS = (StepId.CONNECT, StepId.DATA_MAPPING, *CUTOVER_STEPS)
STEP_OF_STAGE: dict[Stage, StepId] = {
    **{stage: StepId.DATA_SYNC for stage in FLOW_STAGES[: FLOW_STAGES.index(Stage.FENCING)]},
    **{
        stage: StepId.TRAFFIC_SWITCH
        for stage in FLOW_STAGES[
            FLOW_STAGES.index(Stage.FENCING) : FLOW_STAGES.index(Stage.VERIFYING)
        ]
    },
    Stage.VERIFYING: StepId.VERIFY,
}


def route_label(source: str, target: str) -> str:
    return f"{PROVIDER_LABELS.get(source, source)} → {PROVIDER_LABELS.get(target, target)}"


def tool_for(step: StepId, source: str, target: str) -> str | None:
    if step == StepId.CONNECT:
        return "migration.connection" if ONPREM in (source, target) else "클러스터 읽기 계정"
    if step == StepId.DATA_MAPPING:
        return "플랫폼 데이터 매핑"
    if step == StepId.INFRA_CHECK:
        if target != ONPREM:
            return None
        return "migration.harbor_infra · openbao_infra · keycloak_infra · wazuh_infra"
    if step == StepId.DEPLOY:
        return "플랫폼 배포(릴리즈)"
    return TOOLS[step].get((source, target))


def _cutover_status(step: StepId, signals: Signals) -> StepStatus:
    stage = signals.stage
    if stage is None:
        return StepStatus.PENDING
    if stage == Stage.COMPLETED:
        if step == StepId.VERIFY and signals.verify_passed is False:
            return StepStatus.FAILED
        return StepStatus.DONE
    failed = stage in FAILURE_STATUS
    current = STEP_OF_STAGE.get(signals.failed_stage if failed else stage)
    if current is None:
        return StepStatus.FAILED if failed else StepStatus.PENDING
    order = CUTOVER_STEPS.index
    if order(step) < order(current):
        return StepStatus.DONE
    if order(step) > order(current):
        return StepStatus.PENDING
    if failed:
        return StepStatus.FAILED
    return StepStatus.WAITING if stage == Stage.READY else StepStatus.RUNNING


def step_status(step: StepId, signals: Signals) -> StepStatus:
    if step == StepId.CONNECT:
        ready = signals.source_connected and signals.target_connected
        return StepStatus.DONE if ready else StepStatus.PENDING
    if step == StepId.DATA_MAPPING:
        return StepStatus.DONE if signals.mapping_status == "ready" else StepStatus.PENDING
    if step in CUTOVER_STEPS:
        return _cutover_status(step, signals)
    return StepStatus.PENDING


def plan_steps(source: str, target: str, signals: Signals) -> list[PlannedStep]:
    planned = []
    for definition in STEPS:
        tool = tool_for(definition.id, source, target)
        if tool is None:
            planned.append(PlannedStep(definition, None, StepStatus.UNSUPPORTED, False))
            continue
        runnable = definition.id in PLATFORM_STEPS
        planned.append(PlannedStep(definition, tool, step_status(definition.id, signals), runnable))
    return planned


def phase_status(steps: list[PlannedStep]) -> StepStatus:
    statuses = [step.status for step in steps if step.status != StepStatus.UNSUPPORTED]
    if not statuses:
        return StepStatus.UNSUPPORTED
    for status in (StepStatus.FAILED, StepStatus.RUNNING, StepStatus.WAITING):
        if status in statuses:
            return status
    if all(status == StepStatus.DONE for status in statuses):
        return StepStatus.DONE
    return StepStatus.RUNNING if StepStatus.DONE in statuses else StepStatus.PENDING
