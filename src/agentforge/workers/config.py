"""Explicit connection and Worker configuration; no discovery or routing."""

import os
import tomllib
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from agentforge.core.worker import NonBlank, Worker


class ConfigurationError(ValueError):
    """Fixed diagnostics: never echo input configuration or environment values."""


class ProviderConnection(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )

    id: NonBlank
    type: Literal["ollama", "openai_compatible"]
    base_url: HttpUrl = Field(repr=False)
    api_key_env: NonBlank | None = Field(default=None, repr=False)
    # Opt in only on compatible servers accepting stream_options.include_usage.
    stream_usage: bool = False

    @model_validator(mode="after")
    def safe_connection(self) -> Self:
        Worker.endpoint_without_secrets(self.base_url)
        if self.type == "ollama" and (self.api_key_env or self.stream_usage):
            raise ValueError("Unsupported connection settings for this Provider type")
        return self

    def bearer_token(self) -> str | None:
        if self.api_key_env is None:
            return None
        token = os.environ.get(self.api_key_env)
        if (
            not token
            or not token.strip()
            or not token.isascii()
            or any(
                char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token
            )
        ):
            raise ConfigurationError("Required Provider authentication is unavailable")
        return token


class WorkersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    providers: list[ProviderConnection] = Field(default_factory=list)
    workers: list[Worker]

    @model_validator(mode="after")
    def valid_bindings(self) -> Self:
        ids = [worker.id for worker in self.workers]
        if len(ids) != len(set(ids)):
            raise ValueError("worker IDs must be unique")
        connections = {connection.id: connection for connection in self.providers}
        if len(connections) != len(self.providers):
            raise ValueError("Provider IDs must be unique")
        for worker in self.workers:
            if worker.provider_connection is not None:
                connection = connections.get(worker.provider_connection)
                if connection is None or connection.type != worker.provider:
                    raise ValueError("Unknown or mismatched Provider reference")
                if worker.endpoint is not None:
                    raise ValueError(
                        "Referenced Workers cannot define inline endpoints"
                    )
            elif worker.endpoint is None:
                raise ValueError("Inline Workers require an endpoint")
        return self


def load_workers(path: str | Path) -> WorkersConfig:
    """Preferred TOML Worker.provider references a named connection.

    Inline Ollama and named connections are both supported. Programmatic
    WorkersConfig accepts explicitly injected third-party Providers.
    """
    try:
        with Path(path).open("rb") as source:
            data = tomllib.load(source)
        connections = [
            ProviderConnection.model_validate(p) for p in data.get("providers", [])
        ]
        by_id = {p.id: p for p in connections}
        resolved = []
        for entry in data.get("workers", []):
            entry = dict(entry)
            reference = entry.get("provider")
            if "provider_connection" in entry:
                raise ConfigurationError(
                    "Use provider to reference a configured connection"
                )
            if "endpoint" not in entry:
                if reference not in by_id:
                    raise ConfigurationError("Unknown Provider reference")
                entry["provider_connection"] = reference
                entry["provider"] = by_id[reference].type
            elif reference != "ollama":
                raise ConfigurationError(
                    "Inline endpoints require inline Ollama configuration"
                )
            resolved.append(Worker.model_validate(entry))
        config = WorkersConfig(providers=connections, workers=resolved)
        if set(data) - {"workers", "providers"}:
            raise ConfigurationError("Unknown configuration fields")
        for connection in connections:
            connection.bearer_token()  # Validate required environment, never retain it.
        return config
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ConfigurationError(
            "Invalid Worker/Provider configuration or authentication"
        ) from None
