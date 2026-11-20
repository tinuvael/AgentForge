"""Small static composition registry; no plugin discovery or selection policy."""

from collections.abc import Callable

from agentforge.core.inference import Provider
from agentforge.providers.ollama import OllamaProvider
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from agentforge.workers.config import (
    ConfigurationError,
    ProviderConnection,
    WorkersConfig,
)

PROVIDER_TYPES: dict[str, Callable[[ProviderConnection], Provider]] = {
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def validate_provider_bindings(config: WorkersConfig) -> None:
    """Check factory support and authentication without constructing adapters."""
    for connection in config.providers:
        if connection.type not in PROVIDER_TYPES:
            raise ConfigurationError("Unsupported Provider type")
        connection.bearer_token()
    inline = [
        w
        for w in config.workers
        if w.kind == "inference_worker" and w.provider_connection is None
    ]
    if any(w.provider != "ollama" for w in inline):
        raise ConfigurationError("Unsupported inline Provider type")
    if inline:
        if any(p.id == "ollama" for p in config.providers):
            raise ConfigurationError(
                "Provider ID conflicts with inline Provider binding"
            )


def create_providers(config: WorkersConfig) -> dict[str, Provider]:
    # Validate everything before creating instances. Constructors open no clients.
    validate_provider_bindings(config)
    providers = {p.id: PROVIDER_TYPES[p.type](p) for p in config.providers}
    if any(w.endpoint is not None for w in config.workers):
        providers["ollama"] = OllamaProvider()
    return providers
