from typing import Annotated

from pydantic import Field

IdempotencyKey = Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
PlanFingerprint = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
