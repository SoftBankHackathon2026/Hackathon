from fastapi import APIRouter

from controlplane.api.deps import IngestTrafficDep
from controlplane.schemas.traffic import TrafficRoute, TrafficSwitch

router = APIRouter(prefix="/api/v1", tags=["traffic"])


@router.get("/ingest/traffic/{app}")
def route(app: str, routes: IngestTrafficDep) -> TrafficRoute:
    return routes.current(app)


@router.post("/ingest/traffic/{app}")
def switch(app: str, body: TrafficSwitch, routes: IngestTrafficDep) -> TrafficRoute:
    return routes.switch(app, body.target)
