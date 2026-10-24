"""Deterministic HTTP endpoint for shipped adapters; never opens a socket.

Protocol serialization helpers and blocking bodies come from existing adapter
tests. Only the HTTP transport is replaced: runtime, tools and storage stay real.
"""

import json

import httpx

from tests.test_ollama import chat_response
from tests.test_openai_compatible import BytesStream, completion


class ScriptedEndpoint:
    def __init__(self, protocol, operations=()):
        self.protocol = protocol
        self.operations = list(operations)
        self.requests = []
        self.tool_results = []
        self.block = False
        self.body = BytesStream([], block=True)

    def response(self, content="", calls=None):
        if self.protocol == "ollama":
            return chat_response(
                message={
                    "content": content,
                    **({"tool_calls": calls} if calls else {}),
                },
                prompt_eval_count=10,
                eval_count=2,
            )
        return completion(
            content=content,
            calls=calls,
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )

    def __call__(self, request):
        assert request.method == "POST"
        assert request.url.path == (
            "/api/chat" if self.protocol == "ollama" else "/chat/completions"
        )
        payload = json.loads(request.content)
        assert payload["model"] == "synthetic" and payload["stream"] is False
        self.requests.append(payload)
        if self.block:
            self.body.pieces = [json.dumps(self.response("Late answer")).encode()]
            return httpx.Response(200, stream=self.body)
        if payload["messages"][-1]["role"] == "tool":
            result = json.loads(payload["messages"][-1]["content"])
            assert result["ok"], result
            self.tool_results.append(result["result"])
        if not self.operations:
            return httpx.Response(200, json=self.response("Evidence verified."))
        name, arguments = self.operations.pop(0)
        assert name in {t["function"]["name"] for t in payload["tools"]}
        call = {
            "id": f"step-{len(self.requests)}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments
                if self.protocol == "ollama"
                else json.dumps(arguments),
            },
        }
        return httpx.Response(200, json=self.response(calls=[call]))
