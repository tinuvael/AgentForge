"""Normalized tool contracts reject malformed correlation and argument shapes."""

import pytest
from pydantic import ValidationError

from agentforge.core.inference import Message, TokenUsage, ToolCall


@pytest.mark.parametrize("arguments", ["read file", [], None, 1])
def test_tool_calls_require_json_object_arguments(arguments):
    with pytest.raises(ValidationError):
        ToolCall(id="call", name="read_file", arguments=arguments)


@pytest.mark.parametrize(
    "values",
    [
        {"role": "tool", "content": "result"},
        {"role": "tool", "content": "result", "tool_call_id": "call"},
        {"role": "user", "content": "task", "tool_call_id": "call"},
        {
            "role": "system",
            "content": "task",
            "tool_calls": [ToolCall(id="call", name="read_file", arguments={})],
        },
    ],
)
def test_message_roles_enforce_tool_correlation(values):
    with pytest.raises(ValidationError):
        Message(**values)


def test_usage_does_not_coerce_or_fabricate_observations():
    assert TokenUsage().input_tokens is None and TokenUsage().output_tokens is None
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens="1")
    with pytest.raises(ValidationError):
        TokenUsage(output_tokens=-1)
