import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from agentforge.application.service import Application
from agentforge.core.policy import EgressPolicy, PolicyDenied, TaskPolicy
from agentforge.execution import ssh_codex, ssh_codex_repository, ssh_codex_runner
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
elif mode == "cancel_completed":
    assert sys.stdin.buffer.readline() == b"cancel\\n"
    print(json.dumps({"status": "completed", "answer": "AGENTFORGE_SSH_OK"}))
elif mode == "cancel_unconfirmed":
    assert sys.stdin.buffer.readline() == b"cancel\\n"
    print(json.dumps({"status": "stop_unconfirmed"}))
    raise SystemExit(1)
elif mode == "malformed":
    print("private diagnostic")
elif mode == "oversized":
    print("x" * 100_000)
else:
    print(json.dumps({"status": "completed", "answer": "AGENTFORGE_SSH_OK"}))
    if mode == "nonzero":
        raise SystemExit(1)
"""

REPOSITORY = "owner/project"
PIN = "a" * 40

PILOT_SSH = """
import json
import sys
from pathlib import Path

mode, seen = sys.argv[1:]
request = json.loads(sys.stdin.buffer.readline())
with Path(seen).open("a", encoding="utf-8") as log:
    log.write(json.dumps(request) + "\\n")
if mode == "disconnect":
    raise SystemExit(255)
if mode == "mismatch":
    print(json.dumps({"status": "repository_mismatch"}))
    raise SystemExit(1)
if mode == "oversized":
    print("x" * 65537)
    raise SystemExit(0)
if mode in ("cancel", "timeout"):
    assert sys.stdin.buffer.readline() == b"cancel\\n"
    print(json.dumps({"status": "cancelled"}))
    raise SystemExit(0)
print(json.dumps({
    "status": "completed",
    "repository": request["repository"],
    "commit": "b" * 40 if mode == "wrong_commit" else request["commit"],
    "answer": request["question"],
    "sources": [{
        "path": "../secret" if mode == "wrong_path" else "README.md",
        "start": 1,
        "end": 2,
    }],
}))
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
        ("cancel_completed", "cancelled", "cancelled", False),
        ("cancel_unconfirmed", "cancelled", "cancelled", True),
        ("cancel_disconnect", "cancelled", "cancelled", True),
        ("malformed", "failed", "invalid_response", True),
        ("oversized", "failed", "output_limit", True),
        ("nonzero", "failed", "provider_error", True),
    ],
)
def test_claim_execute_and_persist(gateway_app, mode, state, reason, unconfirmed):
    build, project, _, seen = gateway_app
    cancel = mode.startswith("cancel")
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
            if mode == "cancel_completed":
                assert [event.reason for event in progress.timeline] == [
                    "completed",
                    "cancelled",
                ]
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


@pytest.mark.parametrize(
    ("mode", "reason", "detail"),
    [
        ("success", "completed", None),
        ("cancel", "cancelled", None),
        ("timeout", "timeout", None),
        ("mismatch", "security_error", "repository_mismatch"),
        ("disconnect", "provider_error", ssh_codex.STOP_UNCONFIRMED),
        ("wrong_commit", "invalid_response", ssh_codex.STOP_UNCONFIRMED),
        ("wrong_path", "invalid_response", ssh_codex.STOP_UNCONFIRMED),
        ("oversized", "output_limit", ssh_codex.STOP_UNCONFIRMED),
    ],
)
def test_repository_pilot_binding_and_result(
    database, registry, tmp_path, monkeypatch, mode, reason, detail
):
    engine, _ = database
    root = tmp_path / "local"
    root.mkdir()
    project = registry.register_project("pilot", root)
    other = registry.register_project("other", tmp_path)
    registry.set_egress_policy(
        project.id,
        EgressPolicy(mode="cloud_allowed", allowed_connections=("inline:ubuntu",)),
    )
    entry = worker_config(tmp_path, project.id, timeout=1 if mode == "timeout" else 5)
    entry["ssh_repository"] = {"repository": REPOSITORY, "commit": PIN}
    fake = tmp_path / "fake_pilot.py"
    fake.write_text(PILOT_SSH, encoding="utf-8")
    seen = tmp_path / "seen"
    monkeypatch.setattr(
        ssh_codex,
        "repository_arguments",
        lambda _: (sys.executable, "-B", str(fake), mode, str(seen)),
    )
    app = Application(engine, WorkersConfig(workers=[entry]))
    binding = {
        "project_id": project.id,
        "agent_id": ssh_codex_repository.AGENT_ID,
        "worker_id": "ubuntu",
    }
    requests = ["Где точка входа?\nУкажи строки.", "How is the README organized?"]

    async def run():
        await app.start()
        try:
            assert ssh_codex_repository.AGENT_ID in {
                agent.agent_id for agent in app.list_agents().agents
            }
            assert AGENT_ID not in {
                agent.agent_id for agent in app.list_agents().agents
            }
            for override in (
                {"project_id": other.id},
                {"agent_id": "repo_explorer"},
                {"task": "я" * 2049},
                {"task": "nul\0byte"},
            ):
                with pytest.raises(TaskValidationError):
                    app.delegate_task(**(binding | {"task": requests[0]} | override))
            assert not seen.exists()
            for request in requests if mode == "success" else requests[:1]:
                snapshot = app.delegate_task(**binding, task=request)
                if mode == "cancel":
                    while not seen.exists():
                        await asyncio.sleep(0.01)
                    app.cancel_task(task_id=snapshot.task_id)
                result = await app.tasks.wait_task(snapshot.task_id)
                assert result.reason == reason
                assert (
                    app.task_progress(task_id=result.task_id).timeline[0].error_code
                    == detail
                )
                if mode == "success":
                    assert result.final_answer == (
                        f"{request}\n\nSource evidence for {REPOSITORY}@{PIN}\n"
                        "- README.md:1-2"
                    )
                else:
                    assert result.final_answer is None
        finally:
            await app.close()

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert [json.loads(line) for line in seen.read_text().splitlines()] == [
        {"repository": REPOSITORY, "commit": PIN, "question": request}
        for request in (requests if mode == "success" else requests[:1])
    ]


def test_repository_sources_are_pinned_committed_regular_files(tmp_path, monkeypatch):
    executable = shutil.which("git")
    if not executable:
        pytest.skip("Git required for the repository pilot")
    native_run = subprocess.run
    root = tmp_path.resolve()

    def git(*args):
        return native_run(
            [executable, "-C", str(root), *args],
            capture_output=True,
            check=True,
        ).stdout

    git("init", "-b", "main")
    document = root / "README.md"
    document.write_bytes(b"Line one\nLine two\n")
    git("add", "README.md")
    oid = git("rev-parse", ":README.md").decode().strip()
    git("update-index", "--add", "--cacheinfo", f"120000,{oid},symlink.md")
    git(
        "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "a"
    )
    head = git("rev-parse", "HEAD").decode().strip()
    git("remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
    document.write_bytes(b"Private uncommitted text\n")
    (root / "untracked.txt").write_text("Private untracked text")

    def use_local_git(args, **kwargs):
        assert args[0] == "/usr/bin/git"
        return native_run([executable, *args[1:]], **kwargs)

    monkeypatch.setattr(ssh_codex_repository.subprocess, "run", use_local_git)

    def sources(repository=REPOSITORY, commit=head, path="README.md"):
        return ssh_codex_repository.source_files(
            root,
            repository,
            commit,
            [path],
            time.monotonic() + 20,
        )

    snapshot = sources()
    assert snapshot == {"README.md": ["Line one", "Line two"]}
    for arguments in (
        {"repository": "owner/wrong"},
        {"commit": "f" * 40},
        {"path": "symlink.md"},
        {"path": "untracked.txt"},
    ):
        with pytest.raises(ValueError):
            sources(**arguments)
    with pytest.raises(ValueError):
        ssh_codex_repository.checked_answer(
            {
                "answer": "Wrong lines",
                "sources": [{"path": "README.md", "start": 1, "end": 3}],
            },
            snapshot,
        )


@pytest.mark.linux
def test_repository_profile_and_launch_are_gated(tmp_path, monkeypatch):
    import fcntl

    profile = tmp_path / "profile.toml"
    profile.write_text(
        f'repository = "{REPOSITORY}"\ncommit = "{PIN}"\n'
        'root = "/tmp/project"\nfiles = ["README.md"]\n',
        encoding="utf-8",
    )
    profile.chmod(0o600)
    requested = []

    def read_sources(root, repo, commit, paths, deadline):
        assert (root, repo, commit, paths) == (
            Path("/tmp/project"),
            REPOSITORY,
            PIN,
            ["README.md"],
        )
        assert deadline > time.monotonic()
        return {"README.md": ["Real line"]}

    def fake_codex(control, interrupted, launcher, deadline, prompt, *, repository):
        assert repository
        requested.append(prompt)
        return {
            "status": "completed",
            "answer": json.dumps(
                {
                    "answer": "The document begins with Real line.",
                    "sources": [{"path": "README.md", "start": 1, "end": 1}],
                }
            ),
        }

    monkeypatch.setattr(ssh_codex_repository, "source_files", read_sources)

    def run(commit, *, cancel=False):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb", buffering=0) as control:
            try:
                frame = {"repository": REPOSITORY, "commit": commit, "question": "Why?"}
                os.write(writer, json.dumps(frame).encode() + b"\n")
                if cancel:
                    os.write(writer, b"cancel\n")
                return ssh_codex_repository.run(
                    control,
                    lambda: False,
                    Path("/unused"),
                    deadline=time.monotonic() + 5,
                    run_codex=fake_codex,
                    profile_path=profile,
                )
            finally:
                os.close(writer)

    assert run("b" * 40) == {"status": "repository_mismatch"}
    assert run(PIN, cancel=True) == {"status": "cancelled"}
    with profile.open("rb") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        assert run(PIN) == {"status": "busy"}
    assert not requested
    reply = run(PIN)
    assert reply["status"] == "completed" and reply["commit"] == PIN
    assert b"Real line" in requested[0]


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


def test_launcher_rejects_unsupported_platform(monkeypatch, capsys):
    monkeypatch.setattr(ssh_codex_runner.sys, "platform", "darwin")
    assert ssh_codex_runner.main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "stop_unconfirmed"}


@pytest.mark.parametrize("group_disappears", [False, True])
def test_stop_group_requires_esrch_after_eperm(monkeypatch, group_disappears):
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    signals = []
    now = 0.0

    def killpg(pid, sig):
        assert pid == 123
        signals.append(sig)
        if sig == 0:
            if group_disappears and signal.SIGKILL in signals:
                raise ProcessLookupError
            raise PermissionError
        if sig == signal.SIGTERM:
            raise PermissionError

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(ssh_codex_runner.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(ssh_codex_runner, "monotonic", lambda: now)
    monkeypatch.setattr(ssh_codex_runner, "sleep", sleep)
    process = SimpleNamespace(pid=123, poll=lambda: 0)
    assert ssh_codex_runner.stop_group(process) is group_disappears
    assert [sig for sig in signals if sig] == [signal.SIGTERM, signal.SIGKILL]


@pytest.mark.linux
@pytest.mark.parametrize("mode", ["descendant", "ignore_term", "success", "nonzero"])
def test_linux_launcher_contract(tmp_path, mode):
    launcher = tmp_path / "fake_codex"
    pid_file = tmp_path / "started"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:] == ['--ask-for-approval', 'on-request', 'exec',\n"
        "    '--sandbox', 'read-only', '--ignore-user-config',\n"
        "    '--skip-git-repo-check', '--ephemeral', '--json', '-']\n"
        f"assert sys.stdin.read() == {PROMPT + chr(10)!r}\n"
        "assert Path.cwd().name.startswith('agentforge-codex-')\n"
        "assert not list(Path.cwd().iterdir())\n"
        f"mode = {mode!r}\n"
        "if mode in {'success', 'nonzero'}:\n"
        "    print(json.dumps({'type': 'item.completed', 'item':\n"
        f"        {{'type': 'agent_message', 'text': {ANSWER!r}}}}}))\n"
        "    print(json.dumps({'type': 'turn.completed'}))\n"
        "    sys.exit(23 if mode == 'nonzero' else 0)\n"
        "pids = [os.getpid()]\n"
        "if mode == 'descendant':\n"
        "    child = subprocess.Popen(['/bin/sleep', '30'])\n"
        "    signal.signal(signal.SIGTERM, lambda *_: sys.exit(child.wait(3)))\n"
        "    pids.append(child.pid)\n"
        "else:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"started = Path({str(pid_file)!r})\n"
        "pending = started.with_suffix('.tmp')\n"
        "pending.write_text(json.dumps(pids))\n"
        "pending.replace(started)\n"
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
            cancelling = mode in {"descendant", "ignore_term"}
            if cancelling:
                deadline = time.monotonic() + 5
                while not pid_file.exists():
                    assert runner.poll() is None and time.monotonic() < deadline
                    time.sleep(0.025)
                pids = json.loads(pid_file.read_text())
                runner.stdin.write(b"cancel\n")
                runner.stdin.flush()
            assert runner.wait(timeout=5) == 0
            result = json.load(runner.stdout)
            if cancelling:
                assert result == {"status": "cancelled"}
                with pytest.raises(ProcessLookupError):
                    os.killpg(pids[0], 0)
                for pid in pids:
                    with pytest.raises(ProcessLookupError):
                        os.kill(pid, 0)
            elif mode == "success":
                assert result == {"status": "completed", "answer": ANSWER}
            else:
                assert result == {"status": "execution_error"}
        finally:
            if runner.poll() is None:
                runner.stdin.close()
                runner.wait(timeout=5)
