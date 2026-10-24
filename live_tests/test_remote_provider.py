"""Explicit free-endpoint smoke, outside default testpaths and socket-guard suite.

No repository or database is sent; at most one request, 16 output tokens, 15s.
The protected manual workflow supplies configuration only to this step.
"""

import asyncio
import os

import pytest

from agentforge.core.inference import GenerationRequest, Message
from agentforge.core.worker import Worker
from agentforge.providers.factory import create_providers
from agentforge.workers.config import ProviderConnection, WorkersConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTFORGE_LIVE_SMOKE") != "1",
    reason="Explicit free remote endpoint opt-in required; never default CI",
)


def test_one_bounded_remote_generation():
    async def execute():
        providers = {}
        try:
            protocol = os.environ["AGENTFORGE_SMOKE_PROVIDER"]
            endpoint = os.environ["AGENTFORGE_SMOKE_BASE_URL"]
            if not endpoint.startswith("https://"):
                raise ValueError("Remote smoke requires HTTPS")
            connection = ProviderConnection(
                id="smoke",
                type=protocol,
                base_url=endpoint,
                api_key_env="AGENTFORGE_SMOKE_API_KEY"
                if protocol == "openai_compatible"
                and os.environ.get("AGENTFORGE_SMOKE_API_KEY")
                else None,
            )
            worker = Worker(
                id="smoke",
                provider=protocol,
                provider_connection="smoke",
                model=os.environ["AGENTFORGE_SMOKE_MODEL"],
                timeout_seconds=15,
            )
            providers = create_providers(
                WorkersConfig(providers=[connection], workers=[worker])
            )
            async with asyncio.timeout(20):
                result = await providers["smoke"].generate(
                    worker,
                    GenerationRequest(
                        messages=[Message(role="user", content="Reply with OK.")],
                        max_output_tokens=16,
                        timeout_seconds=15,
                    ),
                )
                if not result.content.strip() or result.tool_calls:
                    raise ValueError("Expected nonempty text only")
        except Exception:
            # Never print endpoint/configuration, backend text, headers or tokens.
            raise AssertionError(
                "Remote smoke failed; inspect endpoint privately"
            ) from None
        finally:
            for provider in providers.values():
                if close := getattr(provider, "aclose", None):
                    await close()

    asyncio.run(execute())
