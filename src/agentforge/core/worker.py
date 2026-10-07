"""Concrete inference target configuration, independent of protocol adapters."""

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    field_validator,
    model_validator,
)

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class Worker(BaseModel):
    """Configured capabilities; unknown capabilities stay unknown, not inferred."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )

    id: NonBlank
    provider: NonBlank
    model: NonBlank
    provider_connection: NonBlank | None = None
    # Inline connection; preferred configurations use provider_connection.
    endpoint: HttpUrl | None = Field(default=None, repr=False)
    context_window: int | None = Field(default=None, gt=0)
    supports_streaming: bool = False
    supports_tools: bool | None = None
    deployment_label: NonBlank | None = None
    timeout_seconds: float = Field(default=120.0, gt=0)
    options: dict[str, JsonValue] = Field(default_factory=dict, repr=False)

    @field_validator("endpoint")
    @classmethod
    def endpoint_without_secrets(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (
            value.username or value.password or value.query or value.fragment
        ):
            raise ValueError("endpoint must not contain credentials, query or fragment")
        return value

    @model_validator(mode="after")
    def connection_binding(self):
        if (self.endpoint is None) == (self.provider_connection is None):
            raise ValueError(
                "Supply exactly one connection reference or inline endpoint"
            )
        return self


class WorkerHealth(BaseModel):
    """A point-in-time observation, not mutable state on the configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_available: bool
    model_available: bool | None = None
    error_code: str | None = None

    @property
    def available(self) -> bool:
        return self.backend_available and self.model_available is True
