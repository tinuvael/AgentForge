"""Persistent, authenticated SDK Streamable HTTP MCP; no second executor."""

import ipaddress
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import urlsplit

import anyio
import uvicorn
from mcp.server.auth.middleware.bearer_auth import (
    AuthenticatedUser,
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from agentforge.application.access import ClientApplication
from agentforge.application.service import Application
from agentforge.gateway.auth import CredentialStore, credential_command
from agentforge.mcp.server import _SafeDiagnostics, create_server
from agentforge.web.combined import CompanionConfig, companion_http


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    tls_cert: str | None = None
    tls_key: str | None = field(default=None, repr=False)
    trusted_tls_proxy: bool = False
    max_sessions: int = 64
    session_idle_seconds: float = 300
    max_request_bytes: int = 262_144
    max_connections: int = 128
    max_pending_tasks: int = 256
    watch_seconds: float = 30
    watchers: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        address = ipaddress.ip_address(self.host)
        if address.is_unspecified or address.is_multicast or address.is_global:
            raise ValueError("Bind an explicit loopback or private-network IP")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("TLS requires both certificate and key")
        if not address.is_loopback and not self.tls_cert:
            raise ValueError("Non-loopback listeners require TLS")
        if self.trusted_tls_proxy and not address.is_loopback:
            raise ValueError("TLS termination requires a loopback upstream")
        if not 1 <= self.port <= 65535:
            raise ValueError("Port must be 1–65535")
        if not self.allowed_hosts:
            if not address.is_loopback:
                raise ValueError("Remote TLS requires explicit allowed Host values")
            host = f"[{self.host}]" if ":" in self.host else self.host
            self.allowed_hosts = (f"{host}:{self.port}", f"localhost:{self.port}")
        # Exact authorities/origins, without SDK wildcard suffix matching.
        if any(
            not h or any(c in h for c in "/*@?#\\ \r\n") for h in self.allowed_hosts
        ):
            raise ValueError("Use exact Host authorities")
        if not self.tls_cert and not self.trusted_tls_proxy:
            for authority in self.allowed_hosts:
                hostname = urlsplit("//" + authority).hostname
                try:
                    local = ipaddress.ip_address(hostname or "").is_loopback
                except ValueError:
                    local = hostname == "localhost"
                if not local:
                    raise ValueError(
                        "Remote Host requires TLS or explicit trusted TLS termination"
                    )
        if any(
            not o.startswith(("http://", "https://"))
            or any(c in o for c in "*@?#\\ \r\n")
            for o in self.allowed_origins
        ):
            raise ValueError("Use exact trusted origins")
        if (
            not 1 <= self.max_sessions <= 256
            or not 0 < self.session_idle_seconds <= 3600
        ):
            raise ValueError("Invalid session bounds")
        if not 1024 <= self.max_request_bytes <= 1_048_576:
            raise ValueError("Invalid body limit")
        if (
            not 4 <= self.max_connections <= 512
            or not 1 <= self.max_pending_tasks <= 4096
        ):
            raise ValueError("Invalid capacity")
        if not 0 < self.watch_seconds <= 30:
            raise ValueError("Invalid watch duration")


class RequestBoundary:
    """Bounds HTTP resources before SDK admission; never logs request content."""

    def __init__(self, app, config):
        self.app, self.config = app, config
        self.active = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        headers = request.headers
        status = None
        if scope.get("query_string") or any(
            len(headers.getlist(h)) > 1
            for h in ("host", "origin", "authorization", "mcp-session-id")
        ):
            status = 400
        elif headers.get("host", "").lower() not in {
            h.lower() for h in self.config.allowed_hosts
        }:
            status = 421
        elif (
            "origin" in headers and headers["origin"] not in self.config.allowed_origins
        ):
            status = 403
        elif self.active >= self.config.max_connections:
            status = 503
        if status:
            return await PlainTextResponse("Request refused", status)(
                scope, receive, send
            )
        self.active += 1
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            # Bound slow/chunked bodies before parsing and session creation.
            body = bytearray()
            with anyio.move_on_after(5) as reading:
                more = True
                while more:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.config.max_request_bytes:
                        return await PlainTextResponse("Request too large", 413)(
                            scope, receive, send
                        )
                    body.extend(chunk)
                    more = message.get("more_body", False)
            if reading.cancel_called:
                return await PlainTextResponse("Request timeout", 408)(
                    scope, receive, send
                )
            supplied = False

            async def bounded_receive():
                nonlocal supplied
                if not supplied:
                    supplied = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await receive()

            # A GET stream cannot retain a connection forever. Clients reconnect;
            # durable Tasks and SDK session state have independent lifetimes.
            with anyio.move_on_after(60) as serving:
                await self.app(scope, bounded_receive, safe_send)
            if serving.cancel_called:
                if started:
                    await send({"type": "http.response.body", "body": b""})
                else:
                    await PlainTextResponse("Request timeout", 503)(
                        scope, receive, send
                    )
        finally:
            self.active -= 1


def create_gateway(
    application_factory: Callable[[], Application],
    credentials: CredentialStore,
    config: GatewayConfig,
    *,
    companion: CompanionConfig | None = None,
):
    if companion and companion.director_config:
        from agentforge.director.config import load_config

        director = load_config(companion.director_config)
        target = director.gateway_url
        local_alias = target.host in {"localhost", "127.0.0.1", "[::1]", "::1"}
        if (
            target.port != config.port
            or not (
                target.host == config.host
                or local_alias
                and ipaddress.ip_address(config.host).is_loopback
            )
            or target.scheme != ("https" if config.tls_cert else "http")
        ):
            raise ValueError("Companion Director must target this same Gateway")
    runtime = {}

    def client_access(context):
        # Request metadata is provided by the SDK per message. Do not use a
        # session-inherited ContextVar: it can retain stale authentication.
        user = context.request.scope.get("user") if context.request else None
        if not isinstance(user, AuthenticatedUser) or not user.access_token.subject:
            from agentforge.application.errors import ServiceError

            raise ServiceError("access_denied")
        digest = user.access_token.subject
        return ClientApplication(
            runtime["application"], partial(credentials.principal, digest), config
        )

    server = create_server(
        lambda: runtime["application"], borrowed=True, client_access=client_access
    )

    @asynccontextmanager
    async def lifespan(_web):
        credentials.load()  # Fail closed at startup too, before recovery.
        application = application_factory()
        application.workflows.client_access = lambda client_id: ClientApplication(
            application, partial(credentials.client, client_id), config
        )
        runtime["application"] = application
        manager = StreamableHTTPSessionManager(
            server,
            stateless=False,
            json_response=False,
            event_store=None,  # Task IDs recover durable work, not an SSE replay log.
            security_settings=TransportSecuritySettings(
                allowed_hosts=list(config.allowed_hosts),
                allowed_origins=list(config.allowed_origins),
            ),
            max_sessions=config.max_sessions,
            session_idle_timeout=config.session_idle_seconds,
            max_request_body_size=config.max_request_bytes,
        )
        runtime["manager"] = manager
        try:
            await application.start()
            async with AsyncExitStack() as stack:
                if companion:
                    await stack.enter_async_context(
                        companion_http(application, companion)
                    )
                await stack.enter_async_context(manager.run())
                yield
        finally:
            with anyio.CancelScope(shield=True):
                await application.close()
            runtime.clear()

    class Endpoint:
        async def __call__(self, scope, receive, send):
            await runtime["manager"].handle_request(scope, receive, send)

    web = Starlette(
        routes=[
            Route(
                "/mcp",
                RequireAuthMiddleware(Endpoint(), required_scopes=[]),
                methods=["GET", "POST", "DELETE"],
            )
        ],
        lifespan=lifespan,
    )
    web.router.redirect_slashes = (
        False  # Never redirect HTTPS clients to an HTTP upstream URL.
    )
    protected = AuthenticationMiddleware(
        web,
        backend=BearerAuthBackend(credentials),
    )
    # Starlette runs lifespan through the same wrapper; no UI routes are mounted.
    return RequestBoundary(protected, config)


async def serve(args):
    if args.director_config and not args.companion:
        raise ValueError("Director chat requires --companion")
    config = GatewayConfig(
        host=args.host,
        port=args.port,
        allowed_hosts=tuple(args.allowed_host or ()),
        allowed_origins=tuple(args.allowed_origin or ()),
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
        trusted_tls_proxy=args.trusted_tls_proxy,
        max_sessions=args.max_sessions,
        session_idle_seconds=args.session_idle_seconds,
        max_pending_tasks=args.max_pending_tasks,
    )
    web = create_gateway(
        partial(
            Application.from_config,
            database_url=args.database_url,
            workers_path=args.workers,
            concurrency=args.concurrency,
            coding_path=args.coding,
            specialists_path=args.specialists,
        ),
        CredentialStore(args.auth),
        config,
        companion=CompanionConfig(
            args.companion_host, args.companion_port, args.director_config
        )
        if args.companion
        else None,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            web,
            host=config.host,
            port=config.port,
            workers=1,
            ssl_certfile=config.tls_cert,
            ssl_keyfile=config.tls_key,
            proxy_headers=False,
            access_log=False,
            log_config=None,
            limit_concurrency=config.max_connections,
            backlog=128,
            timeout_keep_alive=5,
            timeout_graceful_shutdown=2,
        )
    )
    await server.serve()
    return 0 if server.started else 1


def run(args):
    if args.operation == "auth-check":
        import json

        print(
            json.dumps(
                {
                    "valid": True,
                    "clients": len(CredentialStore(args.auth).load().clients),
                }
            )
        )
        return 0
    if args.operation != "serve":
        return credential_command(args)
    diagnostics = logging.StreamHandler()
    diagnostics.addFilter(_SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[diagnostics], force=True)
    try:
        return anyio.run(serve, args)
    except KeyboardInterrupt:
        return 0
    except Exception:
        logging.getLogger(__name__).error(
            "service_unavailable: Gateway stopped; check configuration and ownership."
        )
        return 1


def add_parser(groups):
    parser = groups.add_parser(
        "gateway", help="Persistent authenticated MCP service and client credentials"
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    check = commands.add_parser(
        "auth-check", help="Validate private client policy offline"
    )
    check.add_argument("--auth", required=True)
    for operation in ("client-add", "client-rotate", "client-revoke"):
        command = commands.add_parser(operation)
        command.add_argument(
            "--auth", required=True, help="Private operator policy JSON outside Git"
        )
        command.add_argument("--client-id", required=True)
        if operation != "client-revoke":
            command.add_argument(
                "--token-file",
                required=True,
                help="New private output file; never stdout",
            )
    command = commands.add_parser(
        "serve", help="Run until explicitly stopped, independently of Codex"
    )
    from agentforge.mcp.server import add_arguments

    add_arguments(command)
    command.add_argument("--auth", required=True)
    command.add_argument("--host", default="127.0.0.1")
    command.add_argument("--port", type=int, default=8766)
    command.add_argument("--allowed-host", action="append")
    command.add_argument("--allowed-origin", action="append")
    command.add_argument("--tls-cert")
    command.add_argument("--tls-key")
    command.add_argument("--trusted-tls-proxy", action="store_true")
    command.add_argument("--max-sessions", type=int, default=64)
    command.add_argument("--session-idle-seconds", type=float, default=300)
    command.add_argument("--max-pending-tasks", type=int, default=256)
    command.add_argument(
        "--director-config",
        help="Private local Director TOML; requires loopback Companion",
    )
