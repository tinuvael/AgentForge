"""Explicit TOML worker configuration; no discovery, registry or routing."""

import tomllib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from agentforge.core.worker import Worker


class WorkersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workers: list[Worker]

    @model_validator(mode="after")
    def unique_worker_ids(self) -> Self:
        ids = [worker.id for worker in self.workers]
        if len(ids) != len(set(ids)):
            raise ValueError("worker IDs must be unique")
        return self


def load_workers(path: str | Path) -> WorkersConfig:
    """Load only the file explicitly requested by the caller."""
    with Path(path).open("rb") as source:
        return WorkersConfig.model_validate(tomllib.load(source))
