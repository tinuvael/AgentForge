"""Bounded, body-free HTTP observations shared by inference adapters."""

import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from agentforge.core.provider_errors import BackendUnavailable, ProviderRejected
from agentforge.core.rate_limits import MAX_RETRY_AFTER_SECONDS, RateLimitObservation


def rate_limits(
    headers: httpx.Headers, *, now: datetime | None = None
) -> RateLimitObservation | None:
    # Accept only one bounded standard field. No raw header values escape.
    values = headers.get_list("retry-after")
    if len(values) != 1 or len(values[0]) > 128:
        return None
    value = values[0].strip()
    if re.fullmatch(r"[0-9]{1,5}", value):
        seconds = int(value)
    else:
        try:
            target = parsedate_to_datetime(value)
            if target.utcoffset() is None:
                return None
            delay = (target - (now or datetime.now(UTC))).total_seconds()
            if delay < 0:
                return None
            seconds = math.ceil(delay)
        except (ValueError, TypeError, OverflowError):
            return None
    # Do not clamp: an out-of-range observation stays unknown.
    if not 0 <= seconds <= MAX_RETRY_AFTER_SECONDS:
        return None
    return RateLimitObservation(retry_after_seconds=seconds)


def check_status(response: httpx.Response) -> None:
    if 200 <= response.status_code < 300:
        return
    error = BackendUnavailable if response.status_code >= 500 else ProviderRejected
    raise error(
        "Provider HTTP request failed",
        status_code=response.status_code,
        rate_limits=rate_limits(response.headers),
    )
