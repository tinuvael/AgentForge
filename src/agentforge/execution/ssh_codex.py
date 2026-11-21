"""One bounded SSH attempt in the Gateway's existing execution slot."""

import asyncio
import json
from dataclasses import dataclass
from time import monotonic

from agentforge.agents.models import ExecutionResult, TerminationReason, TraceEvent
from agentforge.core.policy import PolicyDenied
from agentforge.execution import ssh_codex_repository
from agentforge.execution.ssh_codex_runner import ANSWER, LAUNCH_COMMAND

MAX_REPLY = 1024
CANCEL_GRACE = 3.0
STOP_UNCONFIRMED = "remote_stop_unconfirmed"


@dataclass(frozen=True)
class Outcome:
    reason: TerminationReason
    answer: str | None = None
    detail: str | None = None


def ssh_arguments(config):
    return (
        str(config.ssh_executable),
        "-F",
        "none",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        f'UserKnownHostsFile="{config.known_hosts_file.as_posix()}"',
        "-o",
        "GlobalKnownHostsFile=none",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ConnectionAttempts=1",
        "-i",
        str(config.identity_file),
        "-l",
        config.user,
        config.host,
        LAUNCH_COMMAND,
    )


async def receive_reply(process, pin):
    limit = ssh_codex_repository.MAX_REPLY if pin else MAX_REPLY
    data = bytearray()
    while chunk := await process.stdout.read(limit + 1 - len(data)):
        data.extend(chunk)
        if len(data) > limit:
            return Outcome("output_limit", detail=STOP_UNCONFIRMED)
    code = await process.wait()
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        document = None
    if code == 0:
        if pin is None and document == {"status": "completed", "answer": ANSWER}:
            return Outcome("completed", ANSWER)
        if pin and isinstance(document, dict) and document.get("status") == "completed":
            try:
                return Outcome("completed", repository_answer(document, pin))
            except ValueError:
                return Outcome("invalid_response", detail=STOP_UNCONFIRMED)
        if document == {"status": "cancelled"}:
            return Outcome("cancelled")
    if code == 1:
        if document == {"status": "timeout"}:
            return Outcome("timeout")
        if document == {"status": "output_limit"}:
            return Outcome("output_limit")
        if document == {"status": "invalid_response"}:
            return Outcome("invalid_response")
        if document == {"status": "execution_error"}:
            return Outcome("provider_error")
        if pin and document == {"status": "repository_mismatch"}:
            return Outcome("security_error", detail="repository_mismatch")
        if pin and document == {"status": "busy"}:
            return Outcome("provider_error", detail="remote_busy")
    reason = "invalid_response" if code == 0 else "provider_error"
    return Outcome(reason, detail=STOP_UNCONFIRMED)


def repository_answer(document, pin):
    if (
        set(document) != {"status", "repository", "commit", "answer", "sources"}
        or document["repository"] != pin.repository
        or document["commit"] != pin.commit
    ):
        raise ValueError("Unexpected repository identity")
    answer, citations = ssh_codex_repository.checked_answer(
        {"answer": document["answer"], "sources": document["sources"]}
    )
    evidence = "\n".join(
        f"- {item['path']}:{item['start']}-{item['end']}" for item in citations
    )
    return f"{answer}\n\nSource evidence for {pin.repository}@{pin.commit}\n{evidence}"


async def wait_for_cancel(token):
    while not token.cancelled:
        await asyncio.sleep(0.05)


async def request_stop(process, reply, *, reason):
    try:
        async with asyncio.timeout(CANCEL_GRACE):
            if not reply.done():
                process.stdin.write(b"cancel\n")
                await process.stdin.drain()
            outcome = await asyncio.shield(reply)
    except (OSError, TimeoutError):
        return Outcome(reason, detail=STOP_UNCONFIRMED)
    if reason == "cancelled" and outcome.detail != STOP_UNCONFIRMED:
        # Preserve a normal terminal reply that raced with cancellation. The
        # repository still honors the cancellation but retains the actual trace.
        return outcome
    return Outcome(reason, detail=outcome.detail)


def repository_arguments(config):
    return (*ssh_arguments(config)[:-1], LAUNCH_COMMAND + " --repository")


async def execute(config, prompt, token, timeout, *, pin=None):
    if token.cancelled:
        return Outcome("cancelled")
    started = monotonic()
    limit = ssh_codex_repository.MAX_REPLY if pin else MAX_REPLY
    if pin:
        if not ssh_codex_repository.valid_question(prompt):
            return Outcome("invalid_configuration")
        request = json.dumps(
            {**pin.model_dump(), "question": prompt}, ensure_ascii=False
        )
    else:
        request = prompt
    try:
        config.check_local_files()
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                *(repository_arguments(config) if pin else ssh_arguments(config)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=limit + 1,
            )
    except TimeoutError:
        return Outcome("timeout")
    except OSError:
        return Outcome("invalid_configuration")

    reply = asyncio.create_task(receive_reply(process, pin))
    cancellation = asyncio.create_task(wait_for_cancel(token))
    try:
        if token.cancelled:
            return Outcome("cancelled")
        # Keep stdin open for cancellation after the request frame.
        process.stdin.write((request + "\n").encode("utf-8"))
        done, _ = await asyncio.wait(
            (reply, cancellation),
            timeout=max(0, timeout - (monotonic() - started)),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if reply in done:
            outcome = reply.result()
            # An unsolicited cancelled envelope is a remote execution failure.
            if outcome.reason == "cancelled" and not token.cancelled:
                return Outcome("provider_error")
            return outcome
        return await request_stop(
            process, reply, reason="cancelled" if token.cancelled else "timeout"
        )
    except asyncio.CancelledError:
        await request_stop(process, reply, reason="cancelled")
        raise
    except OSError:
        return Outcome("provider_error", detail=STOP_UNCONFIRMED)
    finally:
        process.stdin.close()
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        cancellation.cancel()
        # Keep draining until local SSH closes so a paused stdout transport
        # cannot prevent its process wait from completing.
        await asyncio.gather(reply, cancellation, return_exceptions=True)
        while await process.stdout.read(limit):
            pass
        await process.wait()


async def run_task(runtime, task, *, cancellation, observations):
    started = monotonic()
    try:
        worker = runtime.ssh_worker_for(task)
    except ValueError:
        outcome = Outcome("invalid_configuration")
    except PolicyDenied:
        outcome = Outcome("policy_denied")
    else:
        outcome = await execute(
            worker.ssh_codex,
            task.request,
            cancellation,
            worker.timeout_seconds,
            pin=worker.ssh_repository,
        )

    observations.record_trace(
        TraceEvent(
            step=0,
            kind="termination",
            reason=outcome.reason,
            error_code=outcome.detail,
            duration_seconds=max(0, monotonic() - started),
        )
    )
    state = outcome.reason if outcome.reason in {"completed", "cancelled"} else "failed"
    return ExecutionResult(
        project_id=task.project_id,
        agent_id=task.agent_id,
        worker_id=task.worker_id,
        state=state,
        reason=outcome.reason,
        final_answer=outcome.answer,
        steps=0,
        tool_call_count=0,
        tool_output_bytes=0,
        usage=(),
        trace=tuple(observations.trace),
    )
