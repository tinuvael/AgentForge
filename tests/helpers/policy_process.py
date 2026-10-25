"""Independent policy administrator/reader with explicit JSON pipe barriers."""

import asyncio
import contextlib
import io
import json
import os
import sys
from pathlib import Path

from agentforge.cli import main
from agentforge.gateway import auth


def send(event, **fields):
    sys.__stdout__.write(json.dumps({"event": event, **fields}) + "\n")
    sys.__stdout__.flush()


def release():
    if not sys.stdin.readline():
        raise RuntimeError("Parent disconnected")


def run():
    path, operation, identity, output = sys.argv[1:5]
    path, output = Path(path), Path(output)
    if operation == "reader":
        token = output.read_text().strip()
        entered = False

        def holding(read):
            def wrapped(*args):
                nonlocal entered
                if not entered:
                    entered = True
                    send("reading")
                    release()
                return read(*args)

            return wrapped

        if os.name == "nt":
            from agentforge.projects.win32 import Win32

            Win32.read = holding(Win32.read)
        else:
            auth.os.read = holding(auth.os.read)
        result = asyncio.run(auth.CredentialStore(path).verify_token(token))
        send("verified", authorized=result is not None)
        return

    if len(sys.argv) > 5:
        original_replace = auth._replace_policy

        def held_replace(*args):
            send("locked")
            release()
            return original_replace(*args)

        auth._replace_policy = held_replace

    original_save = auth.save_policies
    secrets = []

    def prepared(*args, **kwargs):
        if output.exists():
            secrets.append(output.read_text().strip())
        send("prepared")
        release()
        return original_save(*args, **kwargs)

    auth.save_policies = prepared
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        arguments = ["gateway", operation, "--auth", str(path), "--client-id", identity]
        if operation != "client-revoke":
            arguments += ["--token-file", str(output)]
        code = main(arguments)
    send(
        "done",
        code=code,
        leaked=any(token in stdout.getvalue() + stderr.getvalue() for token in secrets),
    )


if __name__ == "__main__":
    run()
