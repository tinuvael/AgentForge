"""Safe observations, never scheduling policy or account/billing estimates."""

from pydantic import BaseModel, ConfigDict, Field

MAX_RETRY_AFTER_SECONDS = 86_400


class RateLimitObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    retry_after_seconds: int = Field(ge=0, le=MAX_RETRY_AFTER_SECONDS)
