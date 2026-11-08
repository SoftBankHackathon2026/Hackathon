"""클라우드 API 로 읽은 보안 실측값.

수집기(execution.fleet_collector)가 환경마다 읽어 저널에 저장하고, 보안 점검은 실측값이 있으면
DeclaredFacts(신고값)보다 먼저 쓴다. 값의 뜻은 DeclaredFacts 의 같은 이름 필드와 같다
(db_public=True 는 공개되어 있다는 뜻).
"""

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from controlplane.domain.fleet import EnvironmentConfig

# 이번 단계에서 실측으로 옮긴 신고값 필드. 나머지 DeclaredFacts 필드는 신고값만 쓴다.
OBSERVED_FACTS = (
    "db_public",
    "db_tls_required",
    "db_encrypted_at_rest",
    "db_backups",
    "threat_detection",
    "flow_logs",
)
ObservedFact = Literal[
    "db_public",
    "db_tls_required",
    "db_encrypted_at_rest",
    "db_backups",
    "threat_detection",
    "flow_logs",
]

# denied: 권한 없음(AccessDenied, 403). timeout: 응답 없음. no_credentials: 자격 증명 없음.
# not_found: 대상 자원이 없음. api_disabled: 호출한 API 가 프로젝트에서 꺼져 있음(GCP SERVICE_DISABLED).
# unconfigured: 수집 범위(cloud_scope) 미설정. error: 그 밖의 오류.
Failure = Literal[
    "denied", "timeout", "no_credentials", "not_found", "api_disabled", "unconfigured", "error"
]

FAILURE_LABELS: dict[str, str] = {
    "denied": "권한 없음",
    "timeout": "응답 시간 초과",
    "no_credentials": "자격 증명 없음",
    "not_found": "대상 자원 없음",
    "api_disabled": "API 비활성",
    "unconfigured": "실측 범위 미설정",
    "error": "읽기 오류",
}


class Observation(BaseModel):
    """한 신고값 필드의 실측. value 가 None 이면 판단하지 못했고 failure 에 이유가 있다."""

    model_config = ConfigDict(extra="forbid")

    value: bool | None = None
    evidence: str = ""
    failure: Failure | None = None

    @classmethod
    def seen(cls, value: bool, evidence: str) -> "Observation":
        return cls(value=value, evidence=evidence)

    @classmethod
    def failed(cls, failure: Failure, evidence: str) -> "Observation":
        return cls(value=None, failure=failure, evidence=evidence)

    def reason(self) -> str:
        label = FAILURE_LABELS.get(self.failure or "error", "읽기 오류")
        return f"{label}: {self.evidence}" if self.evidence else label


class CloudSecurityFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: datetime
    provider: Literal["aws", "gcp"]
    facts: dict[ObservedFact, Observation] = Field(default_factory=dict)

    def get(self, fact: str) -> Observation | None:
        return self.facts.get(fact)  # type: ignore[call-overload]


class CloudSecurityReader(Protocol):
    """클라우드별 수집기. 한 필드의 오류는 그 필드의 Observation.failure 로 돌려주고,
    읽기 전체가 실패하면 예외를 던진다(수집기가 모든 필드를 실패로 채운다)."""

    def read(self, env: "EnvironmentConfig") -> CloudSecurityFacts: ...


def every_fact_failed(
    provider: Literal["aws", "gcp"], at: datetime, failure: Failure, evidence: str
) -> CloudSecurityFacts:
    return CloudSecurityFacts(
        observed_at=at,
        provider=provider,
        facts={fact: Observation.failed(failure, evidence) for fact in OBSERVED_FACTS},
    )
