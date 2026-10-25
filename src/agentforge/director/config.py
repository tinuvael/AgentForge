"""Server-only director configuration, with no implicit inference destination."""

import ipaddress
import os
import tomllib
from pathlib import Path

from pydantic import Field, HttpUrl, model_validator

from agentforge.core.worker import Worker
from agentforge.director.models import Limits, Record
from agentforge.workers.config import ProviderConnection


class DirectorConfig(Record):
    gateway_url: HttpUrl = Field(repr=False)
    token_env: str = Field(min_length=1, repr=False)
    journal: Path
    inference: ProviderConnection
    model: str = Field(min_length=1, max_length=512)
    options: dict = Field(default_factory=dict, repr=False)
    limits: Limits = Field(default_factory=Limits)

    @model_validator(mode="after")
    def safe_configuration(self):
        Worker.endpoint_without_secrets(self.gateway_url)
        if self.gateway_url.path != "/mcp":
            raise ValueError("Gateway URL must end at /mcp")
        try:
            local = ipaddress.ip_address(self.gateway_url.host.strip("[]")).is_loopback
        except ValueError:
            local = self.gateway_url.host == "localhost"
        if self.gateway_url.scheme != "https" and not local:
            raise ValueError("Remote Gateway requires HTTPS")
        if self.inference.trust != "local":
            raise ValueError("Director inference must be explicitly trusted local")
        self.inference.bearer_token()
        return self

    def token(self):
        value = os.environ.get(self.token_env, "")
        if (
            not value
            or not value.isascii()
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("Director Gateway credential unavailable")
        return value

    def worker(self):
        return Worker(
            id="local-director",
            provider=self.inference.type,
            model=self.model,
            provider_connection=self.inference.id,
            supports_tools=True,
            timeout_seconds=min(self.limits.seconds, 120),
            options=self.options,
        )


def load_config(path):
    try:
        with Path(path).open("rb") as source:
            return DirectorConfig.model_validate(tomllib.load(source))
    except (OSError, ValueError, TypeError):
        raise ValueError("Invalid private Director configuration") from None
