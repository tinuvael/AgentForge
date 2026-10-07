"""Strict wire JSON shared by inference adapters; never echo response data."""

import json
import math

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

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError
        return parsed

    try:
        return json.loads(
            data,
            parse_constant=invalid_constant,
            parse_float=finite_float,
            object_pairs_hook=unique_object,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidProviderResponse("Invalid Provider JSON response") from None
