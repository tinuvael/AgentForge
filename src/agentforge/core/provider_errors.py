"""Safe provider failures; messages exclude backend bodies and request payloads."""


class ProviderError(Exception):
    code = "provider_error"


class BackendUnavailable(ProviderError):
    code = "backend_unavailable"


class ProviderTimeout(ProviderError):
    code = "timeout"


class InvalidProviderResponse(ProviderError):
    code = "invalid_response"


class ProviderRejected(ProviderError):
    code = "rejected"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
