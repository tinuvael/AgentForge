"""Official authenticated MCP client. Only read operations can reconnect/retry."""

from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class DirectorError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@asynccontextmanager
async def connect(config):
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + config.token()},
        trust_env=False,
        follow_redirects=False,
        timeout=15,
    ) as http:
        async with streamable_http_client(
            str(config.gateway_url),
            http_client=http,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
