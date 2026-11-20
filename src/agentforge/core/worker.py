"""Concrete inference target configuration, independent of protocol adapters."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    field_validator,
    model_serializer,
    model_validator,
)

from agentforge.core.capabilities import Capability
from agentforge.core.rate_limits import RateLimitObservation
from agentforge.core.ssh_codex import SSHCodexConfig

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
    kind: Literal["inference_worker", "ssh_codex"] = "inference_worker"
    ssh_codex: SSHCodexConfig | None = Field(default=None, repr=False)
    # Specialist executable Tools will have separate contracts in Issue #52.
    capabilities: dict[Capability, bool | None] = Field(default_factory=dict)
    # Inline Workers only; named connections own trust metadata.
    trust: Literal["local", "cloud", "unknown"] = "unknown"
    context_window: int | None = Field(default=None, gt=0)
    supports_streaming: bool = False
    supports_tools: bool | None = None
    workflow_review_required: bool = False
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
        if "tool_calling" in self.capabilities:
            raise ValueError("Use supports_tools for tool_calling declarations")
        if self.kind == "ssh_codex":
            if (
                self.ssh_codex is None
                or self.provider != "ssh_codex"
                or self.model != "codex-cli"
                or self.trust != "cloud"
                or self.endpoint is not None
                or self.provider_connection is not None
                or self.supports_tools is not False
                or self.supports_streaming
                or self.capabilities
                or self.options
                or self.context_window is not None
                or self.timeout_seconds > 120
            ):
                raise ValueError("Invalid synthetic SSH Codex Worker")
            return self
        if self.ssh_codex is not None or self.provider == "ssh_codex":
            raise ValueError("SSH settings require an SSH Codex Worker")
        if self.provider_connection is not None and self.trust != "unknown":
            raise ValueError("Named connection owns trust classification")
        if (self.endpoint is None) == (self.provider_connection is None):
            raise ValueError(
                "Supply exactly one connection reference or inline endpoint"
            )
        return self

    @model_serializer(mode="wrap")
    def serialize_worker(self, serialize):
        values = serialize(self)
        if self.ssh_codex is None:
            # Existing inference Workers retain their serialized shape.
            values.pop("ssh_codex", None)
        return values

    def binding_payload(self):
        # The default review flag predates some persisted fingerprints.
        exclude = (
            {"workflow_review_required"} if not self.workflow_review_required else set()
        )
        return self.model_dump(mode="json", exclude=exclude)


class WorkerHealth(BaseModel):
    """A point-in-time observation, not mutable state on the configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend_available: bool
    backend_reachable: bool | None = None
    model_available: bool | None = None
    error_code: str | None = None
    rate_limits: RateLimitObservation | None = None

    @property
    def available(self) -> bool:
        return self.backend_available and self.model_available is True
