"""Strict wire JSON shared by inference adapters; never echo response data."""

import json

from agentforge.core.provider_errors import InvalidProviderResponse


def parse_json(data: str | bytes):
    def invalid_constant(_):
        raise ValueError

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return json.loads(
            data, parse_constant=invalid_constant, object_pairs_hook=unique_object
        )
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidProviderResponse("Invalid Provider JSON response") from None
