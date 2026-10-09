"""Official SDK stdio adapter; no inference, filesystem or scheduling logic."""

import argparse
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from importlib.metadata import version

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from agentforge.application.contracts import (
    AgentsPage,
    Capabilities,
    CleanupCodingArguments,
    CouncilArguments,
    DelegateArguments,
    DelegateCouncilArguments,
    NoArguments,
    PageArguments,
    ProjectsPage,
    SafeError,
    Status,
    TaskArguments,
    TaskProgress,
    TaskSnapshot,
    WorkersPage,
)
from agentforge.application.service import Application, ServiceError
from agentforge.coding.models import CodingDiff, CodingError, CodingResult
from agentforge.councils.models import CouncilNotFound, CouncilSnapshot, InvalidCouncil
from agentforge.projects.errors import ProjectNotFound, ProjectStorageError
from agentforge.tasks.models import TaskNotFound, TaskStorageError, TaskValidationError
from agentforge.web.combined import CompanionConfig, companion_http

_MESSAGES = {
    "invalid_arguments": "Supply arguments matching the tool schema.",
    "project_not_found": "Select a registered project_id from list_projects.",
    "worker_not_found": "Select a configured worker_id from list_workers.",
    "agent_not_found": "Select a configured agent_id from list_agents.",
    "invalid_execution_binding": (
        "Check the Agent tools and selected Worker/Provider configuration."
    ),
    "task_not_found": "No durable Task exists for this task_id.",
    "council_not_found": "No durable Council exists for this council_id.",
    "invalid_council": "Supply a valid request and 2 to 16 distinct Worker IDs.",
    "storage_unavailable": (
        "Storage is unavailable; check database configuration and migrations."
    ),
    "service_unavailable": (
        "The Task service is unavailable; check the server lifecycle."
    ),
    "internal_error": "The operation could not be completed.",
    "coding_unavailable": (
        "Coding workspace is unavailable or its ownership/policy check failed."
    ),
}


def safe_error(error: Exception) -> SafeError:
    if isinstance(error, ServiceError):
        code = error.code
    elif isinstance(error, CodingError):
        code = "coding_unavailable"
    elif isinstance(error, ProjectNotFound):
        code = "project_not_found"
    elif isinstance(error, TaskNotFound):
        code = "task_not_found"
    elif isinstance(error, CouncilNotFound):
        code = "council_not_found"
    elif isinstance(error, InvalidCouncil):
        code = "invalid_council"
    elif isinstance(error, (ProjectStorageError, TaskStorageError, SQLAlchemyError)):
        code = "storage_unavailable"
    elif isinstance(error, TaskValidationError):
        code = "invalid_execution_binding"
    else:
        code = "internal_error"
    return SafeError(code=code, message=_MESSAGES[code])


@dataclass(frozen=True)
class ToolContract:
    name: str
    operation: str
    description: str
    arguments: type[BaseModel]
    response: type[BaseModel]
    read_only: bool = True


TOOL_CONTRACTS = (
    ToolContract(
        "get_coding_workspace",
        "get_coding_workspace",
        (
            "Inspect one Task's persisted coding identity and bounded factual "
            "observations; private workspace paths omitted. Validation captures "
            "remain untrusted text."
        ),
        TaskArguments,
        CodingResult,
    ),
    ToolContract(
        "get_coding_diff",
        "get_coding_diff",
        (
            "Retrieve the current central bounded coding diff, including new "
            "files and explicit truncation."
        ),
        TaskArguments,
        CodingDiff,
    ),
    ToolContract(
        "cleanup_coding_workspace",
        "cleanup_coding_workspace",
        (
            "Explicitly remove a terminal Task's managed worktree, retaining "
            "its task branch. Destructive; supply both matching IDs."
        ),
        CleanupCodingArguments,
        CodingResult,
        False,
    ),
    ToolContract(
        "delegate_council",
        "delegate_council",
        "Durably submit the same request independently on 2 to 16 explicitly selected "
        "Workers; returns promptly, without judging or sharing answers.",
        DelegateCouncilArguments,
        CouncilSnapshot,
        False,
    ),
    ToolContract(
        "get_council",
        "get_council",
        "Ordered participant states and completed answers; terminal means all Tasks "
        "are terminal, including mixed outcomes. No request text, trace or reasoning.",
        CouncilArguments,
        CouncilSnapshot,
    ),
    ToolContract(
        "cancel_council",
        "cancel_council",
        "Cancel remaining participants via queued/cooperative Task cancellation; "
        "terminal participants remain unchanged. Safe to repeat.",
        CouncilArguments,
        CouncilSnapshot,
        False,
    ),
    ToolContract(
        "agentforge_status",
        "status",
        "Compact control-plane status; no Worker probes.",
        NoArguments,
        Status,
    ),
    ToolContract(
        "describe_capabilities",
        "capabilities",
        "Factual capabilities and limitations; no routing advice.",
        NoArguments,
        Capabilities,
    ),
    ToolContract(
        "list_projects",
        "list_projects",
        "Page of registered Projects; roots for the trusted local director; "
        "Git not probed.",
        PageArguments,
        ProjectsPage,
    ),
    ToolContract(
        "list_workers",
        "list_workers",
        "Configured Worker capabilities in ID order; no endpoints, health "
        "probes or ranking.",
        PageArguments,
        WorkersPage,
    ),
    ToolContract(
        "list_agents",
        "list_agents",
        "Actual Agent definitions, allowed tools and runtime limits.",
        PageArguments,
        AgentsPage,
    ),
    ToolContract(
        "delegate_task",
        "delegate_task",
        "Submit an explicit Project/Agent/Worker request; return queued "
        "durable identity promptly.",
        DelegateArguments,
        TaskSnapshot,
        False,
    ),
    ToolContract(
        "get_task",
        "get_task",
        "Durable snapshot and completed answer; no trace or reasoning.",
        TaskArguments,
        TaskSnapshot,
    ),
    ToolContract(
        "watch_task",
        "watch_task",
        "Observe one Task using request-scoped MCP progress when a progressToken "
        "is supplied; otherwise return latest safe metadata promptly. No replay "
        "or answer content. Cancelling this watch does not cancel the Task.",
        TaskArguments,
        TaskProgress,
    ),
    ToolContract(
        "cancel_task",
        "cancel_task",
        "Cancel queued work or request cooperative running cancellation; "
        "never force-kill remote inference.",
        TaskArguments,
        TaskSnapshot,
        False,
    ),
)


def create_server(
    application_factory: Callable[[], Application],
    *,
    companion: CompanionConfig | None = None,
) -> Server:
    """Own one shared Application during the SDK server lifespan.

    Use lowlevel SDK primitives so all tool validation/operation errors cross our
    safe boundary. SDK default validation diagnostics can include caller payloads.
    No custom JSON-RPC, HTTP transport or per-request service construction.
    """
    application: Application | None = None

    @asynccontextmanager
    async def lifespan(_server):
        nonlocal application
        if application is not None:
            raise ServiceError("service_unavailable")
        app = application_factory()
        application = app
        try:
            async with AsyncExitStack() as transports:
                try:
                    await app.start()
                    if companion is not None:
                        await transports.enter_async_context(
                            companion_http(app, companion)
                        )
                    yield app
                finally:
                    with anyio.CancelScope(shield=True):
                        # Wake observers before draining HTTP; only this owner closes.
                        await app.close()
        finally:
            application = None

    server = Server(
        "AgentForge",
        version=version("agentforge"),
        lifespan=lifespan,
        instructions=(
            "The director explicitly selects Project, Agent and "
            "Worker(s). Delegate submits; watch_task with MCP progress, "
            "poll get_task/get_council or cancel. "
            "Council participants are independent; the external director judges."
        ),
    )
    contracts = {tool.name: tool for tool in TOOL_CONTRACTS}

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=t.name,
                description=t.description,
                inputSchema=t.arguments.model_json_schema(),
                outputSchema=t.response.model_json_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=t.read_only,
                    destructiveHint=t.name == "cleanup_coding_workspace",
                    idempotentHint=t.name not in {"delegate_task", "delegate_council"},
                    openWorldHint=t.name in {"delegate_task", "delegate_council"},
                ),
            )
            for t in TOOL_CONTRACTS
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            contract = contracts.get(name)
            if contract is None:
                raise ServiceError("invalid_arguments")
            try:
                validated = contract.arguments.model_validate(arguments)
            except ValidationError:
                raise ServiceError("invalid_arguments") from None
            if application is None:
                raise ServiceError("service_unavailable")
            # Synchronous application calls stay on TaskEngine's owning thread.
            fields = {
                field: getattr(validated, field)
                for field in contract.arguments.model_fields
            }
            if name == "watch_task":
                # Public request-local context; no global token/session state.
                context = server.request_context
                token = context.meta.progressToken if context.meta else None
                if token is None:
                    response = application.task_progress(**fields)
                else:
                    async with application.watch_task(**fields) as updates:
                        count = 0
                        async for response in updates:
                            count += 1
                            await context.session.send_progress_notification(
                                progress_token=token,
                                progress=count,
                                message=response.model_dump_json(),
                            )
            else:
                response = getattr(application, contract.operation)(**fields)
            response = contract.response.model_validate(response)
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=response.model_dump_json())
                ],
                structuredContent=response.model_dump(mode="json"),
                isError=False,
            )
        except Exception as error:
            failure = safe_error(error)
            # Actual failed MCP tool result, never a fake successful Task snapshot.
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=failure.model_dump_json())
                ],
                isError=True,
            )

    return server


async def run_stdio(
    *,
    database_url: str,
    workers_path: str,
    concurrency: int,
    coding_path: str | None = None,
    companion: CompanionConfig | None = None,
):
    server = create_server(
        lambda: Application.from_config(
            database_url=database_url,
            workers_path=workers_path,
            concurrency=concurrency,
            coding_path=coding_path,
        ),
        companion=companion,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


class _SafeDiagnostics(logging.Filter):
    """SDK protocol validation logs can include caller payloads or exceptions."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != __name__:
            record.msg = "service_unavailable: MCP protocol/service diagnostic."
            record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def add_arguments(parser) -> None:
    """Shared arguments for the operator CLI and supported module entrypoint."""
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--workers", required=True)
    parser.add_argument("--coding", help="Explicit trusted coding TOML configuration")
    parser.add_argument("--concurrency", type=int, choices=range(1, 33), default=1)
    parser.add_argument(
        "--companion",
        action="store_true",
        help="Serve local Companion in this MCP process",
    )
    parser.add_argument(
        "--companion-host", default="127.0.0.1", help="Loopback IP only"
    )
    parser.add_argument("--companion-port", type=int, default=8765)


def run(arguments) -> int:
    diagnostics = logging.StreamHandler()  # Default stream is stderr.
    diagnostics.addFilter(_SafeDiagnostics())
    logging.basicConfig(level=logging.WARNING, handlers=[diagnostics], force=True)
    try:
        companion = (
            CompanionConfig(arguments.companion_host, arguments.companion_port)
            if arguments.companion
            else None
        )
        anyio.run(
            partial(
                run_stdio,
                database_url=arguments.database_url,
                workers_path=arguments.workers,
                concurrency=arguments.concurrency,
                coding_path=arguments.coding,
                companion=companion,
            )
        )
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Startup/shutdown failures can contain secrets, SQL and local paths.
        logging.getLogger(__name__).error(
            "service_unavailable: MCP server stopped; check configuration "
            "and migrations."
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AgentForge local trusted MCP stdio server"
    )
    add_arguments(parser)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
