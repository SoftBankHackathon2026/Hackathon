from typing import Annotated

from fastapi import APIRouter, Body, Header
from fastapi.responses import PlainTextResponse

from controlplane.api.deps import EnrollerIdentity, EnrollmentsDep, ViewerIdentity
from controlplane.core.errors import OperationNotFound
from controlplane.schemas.enrollments import (
    EnrollmentCreate,
    EnrollmentTicket,
    EnrollmentView,
    Report,
)
from controlplane.services.enrollments import SCRIPTS, script_text

router = APIRouter(prefix="/api/v1", tags=["enrollments"])
SCRIPT_KINDS = {name: kind for kind, name in SCRIPTS.items()}


@router.get("/enrollments/scripts/{name}", response_class=PlainTextResponse)
def enrollment_script(name: str) -> str:
    # 비밀값이 없는 공개 스크립트. 화면은 이 파일의 SHA-256 을 함께 보여 준다.
    if name not in SCRIPT_KINDS:
        raise OperationNotFound("스크립트를 찾지 못했습니다.")
    return script_text(SCRIPT_KINDS[name])


@router.post("/enrollments", status_code=201)
def create_enrollment(
    body: EnrollmentCreate, service: EnrollmentsDep, identity: EnrollerIdentity
) -> EnrollmentTicket:
    return service.create(body.kind, identity)


@router.get("/enrollments")
def list_enrollments(service: EnrollmentsDep, identity: ViewerIdentity) -> list[EnrollmentView]:
    return service.list(identity)


@router.get("/enrollments/{enrollment_id}")
def get_enrollment(
    enrollment_id: str, service: EnrollmentsDep, identity: ViewerIdentity
) -> EnrollmentView:
    return service.get(enrollment_id, identity)


@router.post("/enrollments/{enrollment_id}/complete")
def complete_enrollment(
    enrollment_id: str,
    report: Annotated[Report, Body()],
    service: EnrollmentsDep,
    authorization: str = Header(default=""),
) -> EnrollmentView:
    # 등록 스크립트가 일회용 토큰으로 부른다. 브라우저 세션·Origin 검사 대상이 아니다.
    return service.complete(enrollment_id, authorization, report)
