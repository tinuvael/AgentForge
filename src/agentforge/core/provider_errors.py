"""Safe provider failures; messages exclude backend bodies and request payloads."""

from agentforge.core.rate_limits import RateLimitObservation


class ProviderError(Exception):
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        rate_limits: RateLimitObservation | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.rate_limits = rate_limits


class BackendUnavailable(ProviderError):
    code = "backend_unavailable"


class ProviderTimeout(ProviderError):
    code = "timeout"


class InvalidProviderResponse(ProviderError):
    code = "invalid_response"


class ProviderRejected(ProviderError):
    code = "rejected"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        rate_limits: RateLimitObservation | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, rate_limits=rate_limits)
        self.code = {
            400: "invalid_request",
            401: "authentication_failed",
            403: "authentication_failed",
            404: "not_found",
            429: "rate_limited",
        }.get(self.status_code, "rejected")
