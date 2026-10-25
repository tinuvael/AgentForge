"""Text interfaces to the shared client, without executor configuration."""

import asyncio
import json

from agentforge.director.client import DirectorError
from agentforge.director.config import load_config
from agentforge.director.core import Director


def add_parser(groups):
    parser = groups.add_parser(
        "director", help="Local Director MCP client (no executor)"
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("run", "chat", "resume", "approve", "reject", "clear", "cancel"):
        command = commands.add_parser(name)
        command.add_argument("--config", required=True)
        if name not in {"run", "chat"}:
            command.add_argument("--session", required=True)
        if name == "run":
            command.add_argument("request")
        if name in {"approve", "reject"}:
            command.add_argument("--nonce", required=True)
        if name == "cancel":
            command.add_argument("--task", required=True)


def display(item):
    print(
        json.dumps(
            {
                "session": str(item.id),
                "state": item.state,
                "error": item.error,
                "messages": [m.model_dump() for m in item.messages],
                "tasks": [t.model_dump(mode="json") for t in item.tasks],
                "approval": item.pending.model_dump() if item.pending else None,
                "progress": item.progress,
            },
            ensure_ascii=True,
        )
    )


async def execute(args):
    async with Director(load_config(args.config)) as director:
        item = (
            director.create("cli")
            if args.operation in {"run", "chat"}
            else director.load(args.session, "cli")
        )
        if args.operation == "clear":
            if item.submission or any(
                t.state not in {"completed", "failed", "cancelled", "interrupted"}
                for t in item.tasks
            ):
                raise DirectorError("recover_or_reject_before_clear")
            director.journal.path(item.id).unlink()
            return 0
        if args.operation == "cancel":
            task = next((t for t in item.tasks if str(t.task_id) == args.task), None)
            if task is None or item.pending:
                raise DirectorError("conversation_task_denied")
            director.approval(
                item,
                "cancel_task",
                {"task_id": args.task},
                task.project_id,
                task.task_id,
            )
            display(item)
            return 0
        if args.operation == "chat":
            print("Local Director. /quit exits; approvals use director approve/reject.")
            print("Session: " + str(item.id))
            while True:
                text = await asyncio.to_thread(input, "> ")
                if text.strip() == "/quit":
                    return 0
                item = await director.run(item.id, "cli", text)
                display(item)
                if item.state == "approval":
                    return 0
        else:
            item = await director.run(
                item.id,
                "cli",
                getattr(args, "request", None),
                resume=args.operation == "resume",
                nonce=getattr(args, "nonce", None),
                approve=args.operation == "approve"
                if args.operation in {"approve", "reject"}
                else None,
            )
            display(item)
            return 0 if item.state in {"completed", "approval", "cancelled"} else 1


def run(args):
    try:
        return asyncio.run(execute(args))
    except KeyboardInterrupt:
        print("Director stopped; resume the recorded session to reconcile known Tasks.")
        return 130
    except (DirectorError, ValueError, OSError):
        print(
            '{"error":"director_unavailable",'
            '"hint":"Check configuration, session and approval identity."}'
        )
        return 2
