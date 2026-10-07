"""Named trusted validation execution with bounded output and sanitized env."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentforge.coding.config import ValidationCommand
from agentforge.coding.models import CodingError, CodingLimit, CodingLimits
from agentforge.coding.process import conservative_environment, display_text
from agentforge.coding.tools import ValidationArguments
from agentforge.tools.errors import InvalidToolArgument
from tests.test_coding_workspaces import coding as coding_fixture
from tests.test_coding_workspaces import primary_state, write

coding = coding_fixture


def command(coding, script, *, name="test", timeout=1.0):
    validations = dict(coding.manager.config.validations)
    validations[name] = ValidationCommand(
        argv=(str(Path(sys.executable).resolve()), "-c", script),
        timeout_seconds=timeout,
    )
    coding.manager.config = coding.manager.config.model_copy(
        update={"validations": validations}
    )
    return name


def run_validation(setup, name="check"):
    return setup.session.validate(
        setup.session.task.project_id, ValidationArguments(name=name), 12000
    )


def test_known_command_cwd_environment_and_nonzero(coding, monkeypatch):
    for key in (
        "API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "SSH_AUTH_SOCK",
        "GIT_ASKPASS",
        "OPENAI_API_KEY",
        "PIP_INDEX_URL",
        "OLLAMA_TOKEN",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(key, "PRIVATE_SECRET")
    setup = coding.create(subproject=True)
    before = primary_state(coding.root)
    name = command(
        coding,
        "import os,json,sys; "
        "print(json.dumps({'cwd_name':os.path.basename(os.getcwd()),"
        "'files':sorted(os.listdir('.')),'env':dict(os.environ)})); "
        "sys.stderr.write('failure'); sys.exit(7)",
    )
    result = asyncio.run(run_validation(setup, name))
    assert result["exit_code"] == 7 and not result["timed_out"]
    data = json.loads(result["stdout"])
    assert data["cwd_name"] == "allowed" and data["files"] == ["a.py"]
    assert "PRIVATE_SECRET" not in result["stdout"]
    assert "HOME" not in data["env"] and "SSH_AUTH_SOCK" not in data["env"]
    assert result["stderr"] == "failure"
    assert primary_state(coding.root) == before
    report = coding.manager.get(coding.task.task_id)
    assert len(report.validation_runs) == 1 and report.validation_runs[0].exit_code == 7


@pytest.mark.parametrize(
    "values",
    [
        {"name": "check", "argv": ["sh", "-c", "evil"]},
        {"name": "check", "env": {"TOKEN": "x"}},
        {"name": "check;evil"},
        {"name": "$(evil)"},
        {"name": "../check"},
        {"name": "check\n"},
    ],
)
def test_model_cannot_supply_command_options(values):
    with pytest.raises(ValidationError):
        ValidationArguments.model_validate(values)


def test_unknown_command_and_limit(coding):
    setup = coding.create()
    with pytest.raises(InvalidToolArgument):
        asyncio.run(run_validation(setup, "missing"))
    coding.manager.config = coding.manager.config.model_copy(
        update={"limits": CodingLimits(max_validations=1)}
    )
    assert asyncio.run(run_validation(setup))["exit_code"] == 0
    with pytest.raises(CodingLimit):
        asyncio.run(run_validation(setup))


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_enormous_output_is_bounded(coding, stream):
    setup = coding.create()
    name = command(
        coding, f"import sys; sys.{stream}.write('x'*10000000); sys.{stream}.flush()"
    )
    result = asyncio.run(run_validation(setup, name))
    assert result[f"{stream}_truncated"]
    persisted = coding.manager.get(coding.task.task_id).validation_runs[0]
    assert (
        len(getattr(persisted, stream).encode())
        <= coding.config.limits.max_validation_output_bytes
    )
    assert persisted.exit_code != 0


def test_timeout_and_cancellation(coding):
    setup = coding.create()
    name = command(
        coding, "import time; print('started',flush=True); time.sleep(20)", timeout=0.1
    )
    result = asyncio.run(run_validation(setup, name))
    assert result["timed_out"] and result["exit_code"] != 0
    name = command(
        coding,
        "import time; print('started',flush=True); time.sleep(20)",
        name="cancel",
        timeout=2.0,
    )

    async def execute():
        task = asyncio.create_task(run_validation(setup, name))
        await asyncio.sleep(0.08)
        setup.token.cancel()
        return await task

    result = asyncio.run(execute())
    assert result["cancelled"] and not result["timed_out"]
    assert result["duration_seconds"] < 1.0
    assert setup.workspace.exists()


@pytest.mark.posix
def test_timeout_kills_descendants(coding, tmp_path):
    setup = coding.create()
    marker = tmp_path / "child-survived"
    child = (
        "import time,pathlib; time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')"
    )
    name = command(
        coding,
        "import subprocess,time; "
        f"subprocess.Popen([{str(Path(sys.executable).resolve())!r},"
        f"'-c',{child!r}]); time.sleep(20)",
        timeout=0.1,
    )
    assert asyncio.run(run_validation(setup, name))["timed_out"]
    # Wait through the would-be child's action window to prove process-group kill.
    asyncio.run(asyncio.sleep(0.6))
    assert not marker.exists()


def test_repository_executable_shadowing_and_shell_false(coding, monkeypatch):
    setup = coding.create()
    write(setup, "python", "ATTACK")
    with pytest.raises(ValidationError):
        ValidationCommand(argv=("python", "-m", "pytest"))
    script = str(setup.workspace / "python")
    coding.manager.config = coding.manager.config.model_copy(
        update={"validations": {"shadow": ValidationCommand(argv=(script,))}}
    )
    with pytest.raises(CodingError):
        asyncio.run(run_validation(setup, "shadow"))
    calls = []
    original = subprocess.Popen

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    command(coding, "print('literal ; $(no-shell)')", name="fixed")
    result = asyncio.run(run_validation(setup, "fixed"))
    assert "$(no-shell)" in result["stdout"]
    assert calls[-1][1]["shell"] is False
    assert Path(calls[-1][0][0]).is_absolute()
    assert calls[-1][1]["env"] == conservative_environment()


def test_output_is_plain_text_and_not_markup():
    assert (
        display_text(b"\x1b[31m<script>alert(1)</script>\x1b[0m\0\x7f")
        == "<script>alert(1)</script>"
    )
    assert display_text(b"\x1b]title\x07hello") == "hello"


@pytest.mark.posix
def test_async_cancellation_reaps_process(coding, tmp_path):
    setup = coding.create()
    marker = tmp_path / "survived"
    name = command(
        coding,
        "import time,pathlib; time.sleep(0.5); "
        f"pathlib.Path({str(marker)!r}).write_text('bad')",
        timeout=2.0,
    )

    async def execute():
        task = asyncio.create_task(run_validation(setup, name))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.6)

    asyncio.run(execute())
    assert not marker.exists()
    assert setup.workspace.exists()


def test_utf8_replacement_output_stays_within_byte_cap(coding):
    setup = coding.create()
    name = command(coding, "import os; os.write(1,b'\\xff'*8192)")
    result = asyncio.run(run_validation(setup, name))
    assert result["stdout_truncated"]
    report = coding.manager.get(coding.task.task_id)
    assert (
        len(report.validation_runs[0].stdout.encode())
        <= coding.config.limits.max_validation_output_bytes
    )


def test_operator_config_is_explicit_and_model_options_are_absent(coding, tmp_path):
    from agentforge.coding.config import load_coding

    config = tmp_path / "coding.toml"
    config.write_text(
        f"workspace_parent = {json.dumps(str(coding.parent))}\n"
        f"git_executable = {json.dumps(str(coding.config.git_executable))}\n"
        "[validations.check]\n"
        f"argv = [{json.dumps(str(Path(sys.executable).resolve()))}, "
        "'-c', 'print(1)']\n"
        "timeout_seconds = 1.0\n"
    )
    loaded = load_coding(str(config))
    assert loaded.workspace_parent == coding.parent
    assert loaded.validations["check"].argv[-1] == "print(1)"
    with pytest.raises(ValidationError):
        ValidationCommand(argv=(str(Path(sys.executable).resolve()),), env={"KEY": "x"})
    with pytest.raises(ValidationError):
        type(coding.config).model_validate(
            {
                **coding.config.model_dump(),
                "validations": {"check;evil": loaded.validations["check"]},
            }
        )
