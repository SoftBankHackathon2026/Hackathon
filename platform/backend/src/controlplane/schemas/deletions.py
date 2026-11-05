from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class DeletionEvent(BaseModel):
    phase: str
    message: str
    created_at: datetime


class DeletionView(BaseModel):
    id: str
    state: str
    status: Literal["deleted", "failed", "running"]
    delete_data: bool
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    events: list[DeletionEvent]


def deletion_status(state: str) -> Literal["deleted", "failed", "running"]:
    return state if state in ("deleted", "failed") else "running"


def deletion_view(row: dict[str, Any] | None) -> DeletionView | None:
    if not row:
        return None
    return DeletionView(
        id=row["id"],
        state=row["state"],
        status=deletion_status(row["state"]),
        delete_data=row["delete_data"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        events=[
            DeletionEvent(phase=e["state"], message=e["message"], created_at=e["created_at"])
            for e in row.get("events", [])
        ],
    )
