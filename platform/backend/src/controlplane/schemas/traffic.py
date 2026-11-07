from datetime import datetime

from pydantic import BaseModel, Field

ROUTE_NAME = r"^[a-z0-9][a-z0-9-]{0,62}$"


class TrafficSwitch(BaseModel):
    target: str = Field(pattern=ROUTE_NAME)


class TrafficRoute(BaseModel):
    app: str
    target: str | None
    changed_at: datetime | None = None
