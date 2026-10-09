"""Borrow one started Application for loopback HTTP alongside MCP stdio."""

import asyncio
import ipaddress
import os
import socket
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

import anyio
import uvicorn

from agentforge.application.errors import ServiceError
from agentforge.web.app import create_app


@dataclass(frozen=True)
class CompanionConfig:
    host: str = "127.0.0.1"
    port: int = 8765

    def __post_init__(self):
        try:
            local = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            local = False  # Explicit IP avoids hostname resolution/bind surprises.
        if not local or not 1 <= self.port <= 65535:
            raise ValueError("Companion requires a loopback IP and port 1–65535")


class BorrowedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The MCP entrypoint owns process signals and Application shutdown.
        yield


@asynccontextmanager
async def companion_http(application, config: CompanionConfig):
    host = f"[{config.host}]" if ":" in config.host else config.host
    web = create_app(
        shared_application=application,
        allowed_hosts=tuple(dict.fromkeys(("127.0.0.1", "localhost", "[::1]", host))),
    )
    server = BorrowedServer(
        uvicorn.Config(
            web,
            host=config.host,
            port=config.port,
            workers=1,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    )
    listener = socket.socket(socket.AF_INET6 if ":" in config.host else socket.AF_INET)
    try:
        if os.name != "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((config.host, config.port))
        listener.listen(128)
        listener.setblocking(False)
    except BaseException:
        listener.close()
        raise
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                    raise ServiceError("service_unavailable")
                await asyncio.sleep(0.01)
        yield web
    finally:
        server.should_exit = True
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.wait_for(asyncio.shield(serving), timeout=5)
            except TimeoutError:
                serving.cancel()
                await asyncio.gather(serving, return_exceptions=True)
            finally:
                listener.close()
