from fastapi import APIRouter

from controlplane.api.deps import CostsDep, ViewerIdentity
from controlplane.cost.models import CostOverview

router = APIRouter(prefix="/api/v1", tags=["costs"])


@router.get("/costs")
def costs(service: CostsDep, identity: ViewerIdentity) -> CostOverview:
    """환경별 지금 월 비용과 이전 작업별 대상 환경 비용 추산(USD, 온디맨드 가격표 기준)."""
    return service.overview()
