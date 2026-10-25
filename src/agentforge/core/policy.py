"""Operator policy and bounded Director restrictions, independent of transports."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConnectionID = Annotated[str, Field(min_length=1, max_length=512, pattern=r"\S")]


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EgressPolicy(PolicyModel):
    mode: Literal["local_only", "cloud_allowed"] = "local_only"
    allowed_connections: tuple[ConnectionID, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def valid_allowlist(self):
        if self.mode == "local_only" and self.allowed_connections:
            raise ValueError("Local-only policy cannot authorize cloud connections")
        if len(set(self.allowed_connections)) != len(self.allowed_connections):
            raise ValueError("Duplicate connection authorization")
        return self


class ResourceBudget(PolicyModel):
    # Lifetime of the stable connection identity, not a guessed quota window.
    max_requests: int | None = Field(default=None, strict=True, ge=0, le=1_000_000_000)
    max_tokens: int | None = Field(
        default=None, strict=True, ge=0, le=1_000_000_000_000
    )


class TaskPolicy(ResourceBudget):
    # Omission inherits Project authorization. An explicit policy must be a subset.
    egress: EgressPolicy | None = None


class PolicyStorageError(Exception):
    """Resource admission/evidence storage is unavailable; execution must stop."""


class PolicyDenied(Exception):
    """Fixed safe reasons. No endpoint, configuration or Project text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


DEFAULT_TASK_POLICY = TaskPolicy()
