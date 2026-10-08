"""Trusted local operator adapter. Never exposed as model/MCP tools."""

import argparse
import asyncio
import json
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from agentforge.application.definitions import shipped_agents
from agentforge.coding.config import load_coding
from agentforge.coding.inspection import check_coding_host
from agentforge.coding.service import CodingWorkspaceManager
from agentforge.db.coding import WorkspaceRepository
from agentforge.db.database import create_database_engine, create_session_factory
from agentforge.db.diagnostics import DiagnosticsRepository
from agentforge.db.index import IndexRepository
from agentforge.db.migrate import (
    UnsupportedSchema,
    database_status,
    operator_database_url,
    upgrade_database,
)
from agentforge.db.projects import ProjectRepository
from agentforge.index.models import IndexStorageError
from agentforge.index.service import ProjectIndex
from agentforge.projects.errors import (
    InvalidProjectName,
    InvalidProjectPath,
    ProjectAlreadyRegistered,
    ProjectError,
    ProjectNotFound,
    ProjectStorageError,
    UnsafeProjectPath,
)
from agentforge.projects.service import ProjectRegistry
from agentforge.providers.factory import create_providers, validate_provider_bindings
from agentforge.workers.config import ConfigurationError, load_workers
from agentforge.workers.diagnostic_service import WorkerDiagnosticsService
from agentforge.workers.diagnostics import DiagnosticsUnavailable


class OperatorError(Exception):
    def __init__(self, code, message, exit_code):
        self.code, self.message, self.exit_code = code, message, exit_code


class SafeParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message):
        # argparse's default includes unknown arguments and supplied values.
        raise OperatorError("invalid_arguments", "Use the command's --help.", 2)


def emit(value):
    # JSON escapes terminal controls and keeps output usable in scripts.
    print(json.dumps(value, default=str, ensure_ascii=True, indent=2))


def bounded(value):
    return value[:256] if isinstance(value, str) else value


def _range(minimum, maximum):
    def parse(value):
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise ValueError("Outside bounds")
        return parsed

    return parse


def parser():
    root = SafeParser(
        prog="agentforge",
        description="AgentForge setup and administration for a trusted local operator.",
        epilog=(
            "Setup: edit Worker TOML, db upgrade, project add, optionally project "
            "index, worker config-check, then mcp or web. Use one executor process "
            "per SQLite database. Subcommands have --help."
        ),
    )
    groups = root.add_subparsers(dest="group", required=True)

    def group(name, help_text):
        item = groups.add_parser(name, help=help_text, description=help_text)
        return item.add_subparsers(dest="operation", required=True)

    def database(command):
        command.add_argument(
            "--database-url", required=True, help="Explicit SQLite URL"
        )

    db = group("db", "Explicit database initialization/upgrades and read-only status")
    for name in ("upgrade", "status"):
        database(db.add_parser(name, help=f"Database {name}"))

    project = group("project", "Register, inspect, index and deregister explicit roots")
    add = project.add_parser("add", help="Register one existing directory; no indexing")
    add.add_argument("path", help="Operator-authorized existing local directory")
    add.add_argument("--name", required=True, help="Project display name")
    database(add)
    listing = project.add_parser(
        "list", help="Bounded cached metadata; no Project source/Git I/O"
    )
    database(listing)
    listing.add_argument("--limit", type=_range(1, 100), default=25)
    listing.add_argument("--offset", type=_range(0, 1_000_000), default=0)
    for name, help_text in (
        ("inspect", "Registration and explicit live root/Git observation"),
        ("index", "Explicitly refresh the existing Python Index"),
        ("remove", "Remove registration/cache; durable history and source survive"),
    ):
        command = project.add_parser(name, help=help_text, description=help_text)
        command.add_argument("project_id", type=UUID, help="Registered Project UUID")
        database(command)
        if name == "remove":
            command.add_argument(
                "--yes", action="store_true", help="Explicitly confirm deregistration"
            )

    workers = group(
        "worker",
        "Inspect configuration/diagnostics; explicitly request health or inference",
    )
    for name, help_text in (
        ("list", "Configuration only; does not establish availability"),
        ("config-check", "Validate TOML, authentication and factory bindings offline"),
        ("check", "Cheap health; Ollama model list or compatible not_probed"),
        (
            "probe",
            "Explicit small synthetic inference; sends data to selected Provider",
        ),
        ("diagnostics", "Read persisted observations only; no Provider requests"),
    ):
        command = workers.add_parser(name, help=help_text, description=help_text)
        command.add_argument(
            "--workers", required=True, help="Explicit Worker TOML file"
        )
        if name in {"check", "probe"}:
            command.add_argument("worker_id", help="Explicit configured Worker ID")
            command.add_argument(
                "--database-url",
                required=name == "probe",
                help="Current SQLite database for durable observations",
            )
        if name == "probe":
            command.add_argument(
                "--kind",
                choices=("generation", "tools", "streaming"),
                default="generation",
                help="One bounded inference probe",
            )
        if name == "diagnostics":
            command.add_argument("worker_id", nargs="?", help="Configured Worker ID")
            command.add_argument(
                "--all", action="store_true", help="Read all Workers, paginated"
            )
            database(command)
            command.add_argument("--limit", type=_range(1, 100), default=25)
            command.add_argument("--offset", type=_range(0, 1_000_000), default=0)

    agents = group("agent", "Inspect actual shipped Agent definitions and limits")
    command = agents.add_parser(
        "list", help="Definitions selected by service composition"
    )
    command.add_argument("--coding", help="Opt in to coder with explicit coding TOML")

    coding = group("coding", "Show/validate explicit trusted coding configuration")
    for name in ("show", "check"):
        command = coding.add_parser(name, help=f"Coding configuration {name}")
        command.add_argument(
            "--coding", required=True, help="Explicit coding TOML file"
        )
        if name == "check":
            database(command)

    # Reuse the supported service parsers and lifecycle/security implementations.
    from agentforge.mcp import server as mcp
    from agentforge.web import server as web

    for name, module, help_text in (
        ("mcp", mcp, "Start the MCP stdio executor for a trusted local Director"),
        ("web", web, "Start the trusted local dashboard executor"),
    ):
        command = groups.add_parser(name, help=help_text, description=help_text)
        module.add_arguments(command)
        command.set_defaults(service_run=module.run)
    return root


@contextmanager
def storage_sessions(database_url):
    if database_status(database_url) != "current":
        raise OperatorError(
            "schema_not_current", "Run db status and the explicit db upgrade.", 4
        )
    engine = create_database_engine(operator_database_url(database_url))
    try:
        yield create_session_factory(engine)
    finally:
        engine.dispose()


@contextmanager
def project_services(database_url):
    with storage_sessions(database_url) as sessions:
        registry = ProjectRegistry(ProjectRepository(sessions))
        yield registry, ProjectIndex(registry, IndexRepository(sessions)), sessions


def database_command(args):
    operator_database_url(args.database_url)
    if args.operation == "upgrade":
        upgrade_database(args.database_url)
    try:
        state = database_status(args.database_url)
    except (OSError, SQLAlchemyError):
        emit({"state": "inaccessible"})
        return 3
    emit({"state": state})
    return 0 if state == "current" else 4


def project_command(args):
    with project_services(args.database_url) as (registry, index, _):
        if args.operation == "add":
            project = registry.register_project(args.name, args.path)
            emit({"registration": registration(project)})
        elif args.operation == "list":
            summaries = registry.list_summaries(
                limit=args.limit + 1, offset=args.offset
            )
            emit(
                {
                    "projects": [asdict(p) for p in summaries[: args.limit]],
                    "next_offset": args.offset + args.limit
                    if len(summaries) > args.limit
                    else None,
                    "metadata": "cached_registration_and_index_checkpoint",
                }
            )
        elif args.operation == "inspect":
            inspection = registry.inspect_project(args.project_id)
            emit(
                {
                    "registration": registration(inspection.project),
                    "live_git": asdict(inspection.git),
                }
            )
        elif args.operation == "index":
            status = index.refresh_index(args.project_id)
            emit(
                {
                    "project_id": status.project_id,
                    "indexed_at": status.indexed_at,
                    "observed_head": status.observed_head,
                    "file_count": status.file_count,
                    "symbol_count": status.symbol_count,
                    "parse_failure_count": len(status.failures),
                }
            )
        elif args.operation == "remove":
            project = registry.get_project(args.project_id)
            if not args.yes:
                if not sys.stdin.isatty():
                    raise OperatorError(
                        "confirmation_required",
                        "Use --yes to confirm deregistration.",
                        2,
                    )
                print(
                    f"Remove registration/index cache for {project.id}? "
                    "Source and durable history survive. Type remove to confirm: ",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
                if sys.stdin.readline(128).strip() != "remove":
                    raise OperatorError(
                        "confirmation_required", "No Project removed.", 2
                    )
            registry.remove_project(project.id)
            emit({"removed_project_id": project.id, "durable_history_retained": True})
    return 0


def registration(project):
    return {
        "project_id": project.id,
        "name": project.name,
        "root_path": project.root_path,
        "created_at": project.created_at,
    }


async def observe_worker(config, worker_id, operation, kind, repository):
    providers = create_providers(config)
    service = WorkerDiagnosticsService(config, providers, repository)
    try:
        return (
            await service.check(worker_id)
            if operation == "check"
            else await service.probe(worker_id, kind=kind)
        )
    finally:
        await service.close()
        results = await asyncio.gather(
            *(
                provider.aclose()
                for provider in providers.values()
                if hasattr(provider, "aclose")
            ),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            raise OperatorError("provider_unavailable", "Provider cleanup failed.", 7)


def worker_command(args):
    try:
        config = load_workers(args.workers)
    except OSError:
        raise ConfigurationError("Worker configuration file is unavailable") from None
    validate_provider_bindings(config)
    if args.operation == "config-check":
        emit({"configuration": "valid", "worker_count": len(config.workers)})
    elif args.operation == "list":
        emit(
            {
                "availability": "not_probed",
                "workers": [
                    {
                        key: bounded(value)
                        for key, value in worker.model_dump(
                            include={
                                "id",
                                "provider",
                                "provider_connection",
                                "model",
                                "supports_tools",
                                "supports_streaming",
                                "context_window",
                                "deployment_label",
                            }
                        ).items()
                    }
                    for worker in sorted(config.workers, key=lambda w: w.id)
                ],
            }
        )
    elif args.operation == "diagnostics":
        if bool(args.worker_id) == args.all:
            raise OperatorError("invalid_arguments", "Supply a Worker ID or --all.", 2)
        if args.worker_id and not any(w.id == args.worker_id for w in config.workers):
            raise OperatorError(
                "worker_not_found", "Use worker list for configured IDs.", 5
            )
        with storage_sessions(args.database_url) as sessions:
            service = WorkerDiagnosticsService(
                config, {}, DiagnosticsRepository(sessions)
            )
            emit(
                service.list(limit=args.limit, offset=args.offset).model_dump(
                    mode="json"
                )
                if args.all
                else service.get(args.worker_id).model_dump(mode="json")
            )
    else:
        worker = next((w for w in config.workers if w.id == args.worker_id), None)
        if worker is None:
            raise OperatorError(
                "worker_not_found", "Use worker list for configured IDs.", 5
            )
        context = (
            storage_sessions(args.database_url)
            if args.database_url
            else nullcontext(None)
        )
        with context as sessions:
            observation = asyncio.run(
                observe_worker(
                    config,
                    worker.id,
                    args.operation,
                    getattr(args, "kind", "generation"),
                    DiagnosticsRepository(sessions) if sessions else None,
                )
            )
        if args.operation == "check":
            # Preserve #33's status vocabulary; enrich it with typed observation.
            emit(
                {
                    "worker_id": bounded(worker.id),
                    "status": "unavailable"
                    if observation.status == "failed"
                    else observation.status,
                    "backend_available": observation.backend_available,
                    "model_available": observation.model_available,
                    "backend_reachable": observation.backend_reachable,
                    "error_code": observation.error_code,
                    "checked_at": observation.checked_at.isoformat(),
                    "observation": observation.model_dump(mode="json"),
                    "persisted": sessions is not None,
                }
            )
        else:
            emit(
                {"observation": observation.model_dump(mode="json"), "persisted": True}
            )
        return 7 if observation.status == "failed" else 0

    return 0


def coding_config(path):
    try:
        return load_coding(path)
    except (OSError, ValueError, TypeError, AttributeError):
        raise OperatorError(
            "invalid_coding_config", "Check coding TOML paths, limits and commands.", 2
        ) from None


def config_command(args):
    config = coding_config(args.coding) if args.coding else None
    if args.group == "agent":
        emit(
            {
                "agents": [
                    a.model_dump(exclude={"system_prompt"})
                    for a in shipped_agents(coding_enabled=config is not None)
                ]
            }
        )
    elif args.operation == "show":
        emit(
            {
                "workspace_parent": config.workspace_parent,
                "git_executable": config.git_executable,
                "limits": config.limits.model_dump(),
                "validations": {
                    name: {
                        "timeout_seconds": command.timeout_seconds,
                        "argument_count": len(command.argv),
                    }
                    for name, command in config.validations.items()
                },
            }
        )
    else:
        with project_services(args.database_url) as (registry, _, sessions):
            try:
                manager = CodingWorkspaceManager(
                    registry, WorkspaceRepository(sessions), config
                )
                check_coding_host(manager)
            except (ProjectStorageError, SQLAlchemyError):
                raise
            except Exception:
                raise OperatorError(
                    "invalid_coding_host",
                    "Check private workspace parent, executable access/ancestry "
                    "and root overlap.",
                    2,
                ) from None
        emit(
            {
                "configuration": "valid",
                "host_checks": "passed",
                "commands_executed": False,
                "task_time_checks_still_required": True,
            }
        )
    return 0


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        if args.group in {"mcp", "web"}:
            return args.service_run(args)
        return {
            "db": database_command,
            "project": project_command,
            "worker": worker_command,
            "agent": config_command,
            "coding": config_command,
        }[args.group](args)
    except OperatorError as error:
        code, message, result = error.code, error.message, error.exit_code
    except UnsupportedSchema:
        code, message, result = (
            "unsupported_schema",
            "Select supported storage; unknown databases cannot be adopted.",
            4,
        )
    except ProjectNotFound:
        code, message, result = (
            "project_not_found",
            "Project is not registered; use project list.",
            5,
        )
    except ProjectAlreadyRegistered:
        code, message, result = (
            "project_already_registered",
            "Root is already registered; use project list.",
            5,
        )
    except (InvalidProjectPath, UnsafeProjectPath):
        code, message, result = (
            "unsafe_project_path",
            "Project path is missing, replaced or cannot be accessed safely.",
            6,
        )
    except (ConfigurationError, InvalidProjectName, ValueError, TypeError):
        code, message, result = (
            "invalid_configuration",
            "Check command arguments and configuration files.",
            2,
        )
    except (
        SQLAlchemyError,
        ProjectStorageError,
        IndexStorageError,
        DiagnosticsUnavailable,
    ):
        code, message, result = (
            "storage_unavailable",
            "Check database access and migrations.",
            3,
        )
    except OSError:
        code, message, result = (
            "access_unavailable",
            "Check explicit file paths and access permissions.",
            3,
        )
    except ProjectError:
        code, message, result = (
            "project_unavailable",
            "Project operation could not be completed safely.",
            6,
        )
    except KeyboardInterrupt:
        code, message, result = "interrupted", "Operator command interrupted.", 1
    except Exception:
        code, message, result = (
            "operation_failed",
            "Operator command could not be completed.",
            1,
        )
    print(f"{code}: {message}", file=sys.stderr)
    return result
