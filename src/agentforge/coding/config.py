"""Explicit operator configuration; no Task/model argv, env or root options."""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentforge.coding.models import CodingLimits


class ValidationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    argv: tuple[str, ...] = Field(min_length=1, max_length=32)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    @field_validator("argv")
    @classmethod
    def pinned_executable(cls, value):
        if not Path(value[0]).is_absolute() or any(
            not item or len(item) > 4096 or "\0" in item for item in value
        ):
            raise ValueError("Validation requires an absolute trusted executable")
        if os.name == "nt" and Path(value[0]).suffix.lower() != ".exe":
            raise ValueError("Windows validation requires a direct .exe executable")
        return value


class CodingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_parent: Path
    git_executable: Path
    validations: dict[str, ValidationCommand] = Field(default_factory=dict)
    limits: CodingLimits = Field(default_factory=CodingLimits)

    @field_validator("workspace_parent", "git_executable")
    @classmethod
    def absolute(cls, value):
        if not value.is_absolute():
            raise ValueError(
                "Coding host paths must be operator-selected absolute paths"
            )
        return value

    @field_validator("validations")
    @classmethod
    def command_ids(cls, value):
        import re

        if len(value) > 32 or any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in value
        ):
            raise ValueError("Validation IDs must be bounded simple names")
        return value


def load_coding(path: str) -> CodingConfig:
    with Path(path).open("rb") as file:
        value = tomllib.load(file)
    for key in ("workspace_parent", "git_executable"):
        if key in value:
            value[key] = Path(value[key])
    for command in value.get("validations", {}).values():
        if "argv" in command:
            command["argv"] = tuple(command["argv"])
    return CodingConfig.model_validate(value)
