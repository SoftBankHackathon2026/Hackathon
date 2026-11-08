import json
import re
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from controlplane.core.secrets import secret_from_environment

DEPLOYMENT_MODE_FILE = "deployment-mode.json"
MIN_OPERATOR_TOKEN_LENGTH = 32


class AuthMode(StrEnum):
    OPERATOR = "operator"
    DEMO = "demo"
    LOGIN = "login"


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class HttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "testserver"]
    )
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://127.0.0.1:4320", "http://localhost:4320"]
    )

    @field_validator("allowed_hosts")
    @classmethod
    def exact_hosts(cls, values: list[str]) -> list[str]:
        if not values or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", v) for v in values):
            raise ValueError("Register exact hosts without scheme, port or wildcard")
        return values

    @field_validator("allowed_origins")
    @classmethod
    def exact_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            url = urlsplit(value)
            if (
                url.scheme not in {"https", "http"}
                or not url.hostname
                or url.username
                or url.password
                or url.path
                or url.query
                or url.fragment
                or "*" in value
            ):
                raise ValueError("Register exact HTTP(S) origins without paths or wildcards")
            _ = url.port
        return values

    @classmethod
    def from_environment(cls) -> "HttpSettings":
        return cls.model_validate(_HttpEnvironment().model_dump(exclude_none=True))


class _HttpEnvironment(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONEPORT_", extra="ignore")

    allowed_hosts: Annotated[list[str] | None, NoDecode] = None
    allowed_origins: Annotated[list[str] | None, NoDecode] = None

    @field_validator("allowed_hosts", "allowed_origins", mode="before")
    @classmethod
    def comma_separated(cls, value: object) -> object:
        return _split_csv(value)


class SecuritySettings(BaseSettings):
    """보안 탭 점검 기준값. ONEPORT_SECURITY_* 환경변수로 바꾼다.

    k8s_max_node_skew 는 컨트롤플레인과 각 노드 kubelet 의 마이너 버전 차이 허용값이다.
    """

    model_config = SettingsConfigDict(env_prefix="ONEPORT_SECURITY_", extra="ignore")

    japan_regions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "ap-northeast-1",
            "ap-northeast-3",
            "asia-northeast1",
            "asia-northeast2",
        ]
    )
    # 지원 하한 마이너 버전. 근거(2026-11-08 확인):
    # - 업스트림 1.34 EOL 2026-10-27 (https://kubernetes.io/releases/)
    # - EKS 1.34 표준 지원 종료 2026-12-02, 1.35 이상은 표준 지원 중
    #   (https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html)
    # 다음 갱신: EKS 1.35 표준 지원 종료일 전. 위 EKS 달력에서 날짜를 확인해 올린다.
    k8s_min_minor: str = Field(default="1.35", pattern=r"^\d+\.\d+$")
    k8s_max_node_skew: int = Field(default=1, ge=0, le=3)
    # 클라우드 API 보안 실측 주기(초). 설정은 자주 바뀌지 않고 API 호출 수가 있어 비용 수집과 같은 5분.
    cloud_interval_seconds: int = Field(default=300, ge=60, le=86400)

    @field_validator("japan_regions", mode="before")
    @classmethod
    def comma_separated(cls, value: object) -> object:
        return _split_csv(value)


class PlatformEnvironment(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONEPORT_", extra="ignore")

    auth_mode: AuthMode | None = None
    config: Path | None = None
    runtime_config: Path | None = None
    build_config: Path | None = None
    demo_deploy_config: Path | None = None
    fleet_config: Path | None = None


def deployment_auth_mode(path: Path | None = None) -> AuthMode:
    configured = PlatformEnvironment().auth_mode
    if configured is not None:
        return configured
    packaged = path or Path(str(files("controlplane").joinpath(DEPLOYMENT_MODE_FILE)))
    if not packaged.exists():
        return AuthMode.OPERATOR
    return AuthMode(json.loads(packaged.read_text())["auth_mode"])


def operator_token_from_environment() -> SecretStr:
    token = secret_from_environment("ONEPORT_OPERATOR_TOKEN")
    if len(token.get_secret_value()) < MIN_OPERATOR_TOKEN_LENGTH:
        raise ValueError("ONEPORT_OPERATOR_TOKEN must contain at least 32 characters")
    return token


def ingest_token_from_environment() -> SecretStr | None:
    token = secret_from_environment("ONEPORT_INGEST_TOKEN")
    value = token.get_secret_value()
    if not value:
        return None
    if len(value) < MIN_OPERATOR_TOKEN_LENGTH:
        raise ValueError("ONEPORT_INGEST_TOKEN must contain at least 32 characters")
    return token
