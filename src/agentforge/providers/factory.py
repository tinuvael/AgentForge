"""Small static composition registry; no plugin discovery or selection policy."""

from agentforge.providers.ollama import OllamaProvider
from agentforge.providers.openai_compatible import OpenAICompatibleProvider
from agentforge.workers.config import ConfigurationError, WorkersConfig

PROVIDER_TYPES = {
    "ollama": OllamaProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def validate_provider_bindings(config: WorkersConfig) -> None:
    """Check factory support and authentication without constructing adapters."""
    for connection in config.providers:
        if connection.type not in PROVIDER_TYPES:
            raise ConfigurationError("Unsupported Provider type")
        connection.bearer_token()
    if any(
        w.provider_connection is None and w.provider != "ollama" for w in config.workers
    ):
        raise ConfigurationError("Unsupported inline Provider type")
    if any(w.provider_connection is None for w in config.workers):
        if any(p.id == "ollama" for p in config.providers):
            raise ConfigurationError(
                "Provider ID conflicts with inline Provider binding"
            )


def create_providers(config: WorkersConfig):
    # Validate everything before creating instances. Constructors open no clients.
    validate_provider_bindings(config)
    providers = {p.id: PROVIDER_TYPES[p.type](p) for p in config.providers}
    if any(w.provider_connection is None for w in config.workers):
        providers["ollama"] = OllamaProvider()
    return providers
