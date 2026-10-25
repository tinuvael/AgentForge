"""Declarations and scoped observations, never model quality claims."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Capability = Literal[
    "coding", "reasoning", "tool_calling", "vision", "image", "3d", "video", "speech"
]
CAPABILITIES: tuple[Capability, ...] = (
    "coding",
    "reasoning",
    "tool_calling",
    "vision",
    "image",
    "3d",
    "video",
    "speech",
)
Support = Literal["supported", "unsupported", "unknown"]


class CapabilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    support: Support = "unknown"
    source: Literal["declared", "measured", "unknown"] = "unknown"
    provenance: Literal["operator_configuration", "synthetic_tool_probe", "none"] = (
        "none"
    )
    observed_at: datetime | None = None
    limitation: str = Field(max_length=300)


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # A positive synthetic observation never promotes unknown/declared support.
    support: Support
    declaration: CapabilityEvidence
    observations: tuple[CapabilityEvidence, ...] = Field(default=(), max_length=1)


def support(value: bool | None) -> Support:
    return "unknown" if value is None else "supported" if value else "unsupported"


def describe(worker, diagnostics=None) -> dict[Capability, CapabilityDescriptor]:
    result = {}
    for capability in CAPABILITIES:
        value = (
            worker.supports_tools
            if capability == "tool_calling"
            else worker.capabilities.get(capability)
        )
        declared = support(value)
        observations = ()
        effective = declared
        if capability == "tool_calling" and diagnostics and diagnostics.tools.latest:
            probe = diagnostics.tools.latest
            if probe.tool_call_success is not None:
                measured = support(probe.tool_call_success)
                observations = (
                    CapabilityEvidence(
                        support=measured,
                        source="measured",
                        provenance="synthetic_tool_probe",
                        observed_at=probe.checked_at,
                        limitation=(
                            "One synthetic protocol exchange; "
                            "no quality or universal capability claim."
                        ),
                    ),
                )
                if measured == "unsupported" and declared == "supported":
                    effective = "unknown"
        result[capability] = CapabilityDescriptor(
            support=effective,
            declaration=CapabilityEvidence(
                support=declared,
                source="unknown" if value is None else "declared",
                provenance="none" if value is None else "operator_configuration",
                limitation="Operator declaration is not verified model quality."
                if value is not None
                else "No capability declaration.",
            ),
            observations=observations,
        )
    return result
