"""Standalone Linux launcher. Install as ~/bin/agentforge-codex-worker."""

import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic, sleep

PROMPT = "Reply with exactly AGENTFORGE_SSH_OK. Do not use tools."
ANSWER = "AGENTFORGE_SSH_OK"
AGENT_ID = "ssh_codex_synthetic"
LAUNCH_COMMAND = 'exec /usr/bin/python3 "$HOME/bin/agentforge-codex-worker"'
MAX_CODEX_OUTPUT = 256 * 1024
REMOTE_TIMEOUT = 120


def codex_answer(output):
    answer = None
    completed = False
    for line in output.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind in {"error", "turn.failed"}:
            return None
        if kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                return None
            if item.get("type") == "agent_message":
                if completed:
                    return None
                answer = item.get("text")
        if kind == "turn.completed":
            completed = answer is not None
    return ANSWER if completed and answer == ANSWER else None


def stop_group(process):
    # An acknowledgement requires the original process group to be gone.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            process.poll()
            return True
        deadline = monotonic() + 0.6
        while monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return True
            sleep(0.025)
    return False


def observe(process, control, interrupted, deadline):
    output = bytearray()
    with selectors.DefaultSelector() as ready:
        ready.register(control, selectors.EVENT_READ, "control")
        ready.register(process.stdout, selectors.EVENT_READ, "output")
        while not interrupted():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return {"status": "timeout"}
            for key, _ in ready.select(min(remaining, 0.1)):
                if key.data == "control":
                    # The only post-request command is cancellation. EOF also stops.
                    os.read(control.fileno(), 64)
                    return {"status": "cancelled"}
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    ready.unregister(process.stdout)
                output.extend(chunk)
                if len(output) > MAX_CODEX_OUTPUT:
                    return {"status": "output_limit"}
            if len(ready.get_map()) == 1 and process.poll() is not None:
                if process.returncode:
                    return {"status": "execution_error"}
                try:
                    answer = codex_answer(output)
                except (ValueError, RecursionError):
                    answer = None
                if answer is None:
                    return {"status": "invalid_response"}
                return {"status": "completed", "answer": answer}
    return {"status": "cancelled"}


def execute(control, interrupted, launcher):
    deadline = monotonic() + REMOTE_TIMEOUT
    expected = (PROMPT + "\n").encode()
    request = bytearray()
    # Unbuffered input avoids consuming a cancellation frame with the request.
    with selectors.DefaultSelector() as ready:
        ready.register(control, selectors.EVENT_READ)
        while len(request) < len(expected):
            if interrupted() or monotonic() >= deadline:
                return {"status": "timeout"}
            if ready.select(0.1):
                chunk = os.read(control.fileno(), len(expected) - len(request))
                if not chunk:
                    return {"status": "invalid_response"}
                request.extend(chunk)
    if request != expected:
        return {"status": "invalid_response"}
    with tempfile.TemporaryDirectory(prefix="agentforge-codex-") as directory:
        process = subprocess.Popen(
            [
                str(launcher),
                "--ask-for-approval",
                "on-request",
                "exec",
                "--sandbox",
                "read-only",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--ephemeral",
                "--json",
                "-",
            ],
            cwd=directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={"HOME": str(Path.home()), "PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        try:
            process.stdin.write(expected)
            process.stdin.close()
            result = observe(process, control, interrupted, deadline)
        except OSError:
            result = {"status": "execution_error"}
        finally:
            stopped = stop_group(process)
            process.stdin.close()
            process.stdout.close()
        return result if stopped else {"status": "stop_unconfirmed"}


def main():
    interrupted = False

    def interrupt(*_):
        nonlocal interrupted
        interrupted = True

    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupt)
    try:
        result = execute(
            sys.stdin.buffer, lambda: interrupted, Path.home() / "bin/codex-remote"
        )
    except (OSError, ValueError):
        result = {"status": "stop_unconfirmed"}
    print(json.dumps(result), flush=True)
    return 0 if result["status"] in {"completed", "cancelled"} else 1


if __name__ == "__main__":
    sys.exit(main())
