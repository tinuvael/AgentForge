import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agentforge.application.service import Application
from agentforge.core.policy import EgressPolicy, PolicyDenied, TaskPolicy
from agentforge.execution import ssh_codex, ssh_codex_runner
from agentforge.execution.ssh_codex_runner import AGENT_ID, ANSWER, PROMPT
from agentforge.providers.ollama import OllamaProvider
from agentforge.tasks.models import TaskValidationError
from agentforge.workers.config import WorkersConfig, load_workers

FAKE_SSH = """
import json
import sys
from pathlib import Path

mode, seen = sys.argv[1:]
request = sys.stdin.buffer.readline()
with Path(seen).open("ab") as log:
    log.write(request)
if mode == "disconnect":
    raise SystemExit(255)
if mode == "timeout":
    sys.stdin.buffer.readline()
    raise SystemExit(255)
if mode == "cancel":
    assert sys.stdin.buffer.readline() == b"cancel\\n"
    print(json.dumps({"status": "cancelled"}))
elif mode == "malformed":
    print("private diagnostic")
elif mode == "oversized":
    print("x" * 100_000)
else:
    print(json.dumps({"status": "completed", "answer": "AGENTFORGE_SSH_OK"}))
    if mode == "nonzero":
        raise SystemExit(1)
"""


def worker_config(tmp_path, project_id, *, timeout=5):
    identity = tmp_path / "private key"
    known_hosts = tmp_path / "known hosts"
    identity.touch()
    known_hosts.touch()
    return {
        "id": "ubuntu",
        "kind": "ssh_codex",
        "provider": "ssh_codex",
        "model": "codex-cli",
        "trust": "cloud",
        "supports_tools": False,
        "timeout_seconds": timeout,
        "ssh_codex": {
            "host": "192.0.2.10",
            "user": "synthetic",
            "project_id": str(project_id),
            "ssh_executable": sys.executable,
            "identity_file": str(identity),
            "known_hosts_file": str(known_hosts),
        },
    }


def test_static_config_and_literal_ssh_arguments(tmp_path):
    entry = worker_config(tmp_path, uuid4())
    section = entry.pop("ssh_codex")
    file = tmp_path / "workers.toml"
    file.write_text(
        "[[workers]]\n"
        + "".join(f"{k} = {json.dumps(v)}\n" for k, v in entry.items())
        + "[workers.ssh_codex]\n"
        + "".join(f"{k} = {json.dumps(v)}\n" for k, v in section.items()),
        encoding="utf-8",
    )
    config = load_workers(file)
    ssh = config.workers[0].ssh_codex
    ssh.check_local_files()
    args = ssh_codex.ssh_arguments(ssh)
    assert args[:4] == (sys.executable, "-F", "none", "-T")
    assert "StrictHostKeyChecking=yes" in args
    assert "IdentityAgent=none" in args
    assert "ForwardAgent=no" in args
    assert "GlobalKnownHostsFile=none" in args
    assert f'UserKnownHostsFile="{ssh.known_hosts_file.as_posix()}"' in args
    assert args[-2:] == ("192.0.2.10", ssh_codex_runner.LAUNCH_COMMAND)
    assert PROMPT not in args
    ssh.identity_file.unlink()
    with pytest.raises(OSError, match="Required SSH file"):
        ssh.check_local_files()


@pytest.fixture
def gateway_app(database, registry, tmp_path, monkeypatch):
    engine, _ = database
    root = tmp_path / "synthetic"
    root.mkdir()
    project = registry.register_project("synthetic", root)
    other = registry.register_project("unrelated", tmp_path)
    for identity in (project.id, other.id):
        registry.set_egress_policy(
            identity,
            EgressPolicy(mode="cloud_allowed", allowed_connections=("inline:ubuntu",)),
        )
    fake = tmp_path / "fake_ssh.py"
    fake.write_text(FAKE_SSH, encoding="utf-8")
    seen = tmp_path / "seen"

    def build(mode, timeout=5, providers=None):
        monkeypatch.setattr(
            ssh_codex,
            "ssh_arguments",
            lambda _: (sys.executable, "-B", str(fake), mode, str(seen)),
        )
        config = WorkersConfig(
            workers=[
                worker_config(tmp_path, project.id, timeout=timeout),
                {
                    "id": "http",
                    "provider": "ollama",
                    "model": "local",
                    "endpoint": "http://127.0.0.1:11434",
                },
            ]
        )
        return Application(engine, config, providers=providers)

    return build, project, other, seen


@pytest.mark.parametrize(
    ("mode", "state", "reason", "unconfirmed"),
    [
        ("success", "completed", "completed", False),
        ("disconnect", "failed", "provider_error", True),
        ("timeout", "failed", "timeout", True),
        ("cancel", "cancelled", "cancelled", False),
        ("cancel_disconnect", "cancelled", "cancelled", True),
        ("malformed", "failed", "invalid_response", True),
        ("oversized", "failed", "output_limit", True),
        ("nonzero", "failed", "provider_error", True),
    ],
)
def test_claim_execute_and_persist(gateway_app, mode, state, reason, unconfirmed):
    build, project, _, seen = gateway_app
    cancel = mode in {"cancel", "cancel_disconnect"}
    app = build(
        "timeout" if mode == "cancel_disconnect" else mode,
        timeout=1 if mode == "timeout" else 5,
    )

    async def run():
        await app.start()
        try:
            snapshot = app.delegate_task(
                project_id=project.id,
                agent_id=AGENT_ID,
                worker_id="ubuntu",
                task=PROMPT,
            )
            assert snapshot.state == "queued"
            if cancel:
                while not seen.exists():
                    await asyncio.sleep(0.01)
                assert app.get_task(task_id=snapshot.task_id).state == "running"
                app.cancel_task(task_id=snapshot.task_id)
            result = await app.tasks.wait_task(snapshot.task_id)
            assert app.get_task(task_id=result.task_id).state == state
            progress = app.task_progress(task_id=result.task_id)
            diagnostic = any(
                event.error_code == ssh_codex.STOP_UNCONFIRMED
                for event in progress.timeline
            )
            assert diagnostic == unconfirmed
            assert result.final_answer == (ANSWER if state == "completed" else None)
            assert result.reason == reason
            assert "private diagnostic" not in result.model_dump_json()
        finally:
            await app.close()

    asyncio.run(asyncio.wait_for(run(), timeout=12))
    assert seen.read_bytes() == (PROMPT + "\n").encode()


def test_synthetic_binding_and_http_discovery(gateway_app):
    build, project, other, seen = gateway_app
    app = build("success")

    async def run():
        await app.start()
        try:
            assert {w.worker_id: w.kind for w in app.list_workers().workers} == {
                "http": "inference_worker",
                "ubuntu": "ssh_codex",
            }
            binding = dict(
                project_id=project.id,
                agent_id=AGENT_ID,
                worker_id="ubuntu",
                task=PROMPT,
            )
            for override in (
                {"project_id": other.id},
                {"agent_id": "repo_explorer"},
                {"worker_id": "http"},
                {"task": PROMPT + " extra"},
            ):
                with pytest.raises(TaskValidationError):
                    app.delegate_task(**(binding | override))
            with pytest.raises(PolicyDenied):
                app.delegate_task(**binding, task_policy=TaskPolicy(max_requests=1))
            assert not seen.exists()
            assert app.tasks.list_tasks() == []
        finally:
            await app.close()

    asyncio.run(run())


def test_ssh_and_ollama_use_the_same_task_engine(gateway_app):
    build, project, _, seen = gateway_app
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "local",
                "message": {"role": "assistant", "content": "HTTP answer."},
                "done": True,
                "done_reason": "stop",
            },
        )

    app = build(
        "success",
        providers={"ollama": OllamaProvider(transport=httpx.MockTransport(handle))},
    )

    async def run():
        await app.start()
        try:
            for agent, worker, prompt, expected in (
                (AGENT_ID, "ubuntu", PROMPT, ANSWER),
                ("general_agent", "http", "Local-only question.", "HTTP answer."),
            ):
                submitted = app.delegate_task(
                    project_id=project.id, agent_id=agent, worker_id=worker, task=prompt
                )
                finished = await app.tasks.wait_task(submitted.task_id)
                assert finished.state == "completed", finished.error_code
                assert finished.final_answer == expected
        finally:
            await app.close()

    asyncio.run(asyncio.wait_for(run(), timeout=12))
    assert [request["model"] for request in requests] == ["local"]
    assert seen.read_bytes() == (PROMPT + "\n").encode()


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([{"type": "turn.completed"}], None),
        (
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": ANSWER},
                }
            ],
            None,
        ),
        (
            [
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": ANSWER},
                },
                {"type": "turn.completed"},
            ],
            ANSWER,
        ),
    ],
)
def test_codex_jsonl_requires_final_message_and_turn(events, expected):
    encoded = "\n".join(map(json.dumps, events)).encode()
    assert ssh_codex_runner.codex_answer(encoded) == expected


@pytest.mark.posix
def test_linux_launcher_cancels_group(tmp_path):
    import subprocess
    import time

    launcher = tmp_path / "fake_codex"
    pid_file = tmp_path / "started"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:4] == ['--ask-for-approval', 'on-request', 'exec']\n"
        "assert sys.argv[4:6] == ['--sandbox', 'read-only']\n"
        f"assert sys.stdin.read() == {PROMPT + chr(10)!r}\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    launcher.chmod(0o700)
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from agentforge.execution.ssh_codex_runner import execute\n"
        "print(json.dumps(execute(sys.stdin.buffer, lambda: False, Path(sys.argv[1]))))"
    )
    with subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(launcher)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    ) as runner:
        try:
            runner.stdin.write((PROMPT + "\n").encode())
            runner.stdin.flush()
            deadline = time.monotonic() + 5
            while not pid_file.exists():
                assert time.monotonic() < deadline
                time.sleep(0.025)
            runner.stdin.write(b"cancel\n")
            runner.stdin.flush()
            assert runner.wait(timeout=5) == 0
            assert json.load(runner.stdout) == {"status": "cancelled"}
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
        finally:
            if runner.poll() is None:
                runner.stdin.close()
                runner.wait(timeout=5)
