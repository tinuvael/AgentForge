"""Disposable Codex homes only; no operator configuration or live inference."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agentforge import codex_integration as integration
from agentforge.cli import main


@pytest.fixture
def setup(tmp_path, database, monkeypatch):
    home = tmp_path / "operator home" / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "operator home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "operator home"))
    workers = tmp_path / "workers with spaces.toml"
    workers.write_text(
        """[[workers]]
id = "local"
provider = "ollama"
model = "synthetic"
endpoint = "http://localhost:11434"
supports_tools = true
""",
        encoding="utf-8",
    )
    command = [
        "codex",
        "install",
        "--single-client",
        "--python",
        sys.executable,
        "--installation",
        str(Path(integration.__file__).parent.parent),
        "--database",
        database[1].database,
        "--workers",
        str(workers),
        "--cwd",
        str(tmp_path),
        "--instructions",
    ]
    return home, command


def invoke(command, capsys, code=0):
    assert main(command) == code
    output = capsys.readouterr()
    return json.loads(output.out) if code == 0 else output.err


def test_clean_install_dry_run_idempotent_and_uninstall(setup, capsys):
    home, command = setup
    preview = invoke(command + ["--dry-run"], capsys)
    assert len(preview["changed_files"]) == 2 and preview["backups"] == []
    assert not home.exists()
    installed = invoke(command, capsys)
    assert installed["backups"] == []
    config = (home / "config.toml").read_bytes()
    entry = tomllib.loads(config.decode())["mcp_servers"]["agentforge"]
    assert entry["command"] == sys.executable
    assert entry["required"] is False
    assert entry["default_tools_approval_mode"] == "writes"
    assert entry["disabled_tools"] == ["cleanup_coding_workspace"]
    assert "env" not in entry and "url" not in entry
    assert len(entry["args"]) == 7  # argv, never a shell command
    assert "cannot guarantee a tool" in (home / "AGENTS.md").read_text()
    again = invoke(command, capsys)
    assert again["changed_files"] == again["backups"] == []
    inspected = invoke(["codex", "inspect"], capsys)
    assert inspected["managed_registration"] == entry
    assert inspected["managed_instructions"]
    assert inspected["server_availability"] == "not_probed"
    assert "#44" in inspected["warning"]
    assert invoke(["codex", "uninstall", "--dry-run"], capsys)["changed_files"]
    assert (home / "config.toml").read_bytes() == config
    removed = invoke(["codex", "uninstall"], capsys)
    assert len(removed["backups"]) == 2
    assert (home / "config.toml").read_bytes() == b""
    assert (home / "AGENTS.md").read_bytes() == b""
    assert invoke(["codex", "uninstall"], capsys)["changed_files"] == []


COMPLEX = b'''# operator comment
model = "example"
developer_instructions = """Keep my own instructions.
With a second line."""
[profiles.work]
model_reasoning_effort = "high"
[mcp_servers.other]
command = "other-python"
args = ["--x", "literal $()"]
[mcp_servers.other.env]
PRIVATE = "retain-secret-without-printing"
[projects."/some/repo"]
trust_level = "trusted"'''.replace(b"\n", b"\r\n")


def test_complex_config_instructions_backups_updates_and_exact_restoration(
    setup, capsys
):
    home, command = setup
    home.mkdir(parents=True)
    policy = b"# Existing user instructions\r\nKeep all bytes.\r\n"
    (home / "config.toml").write_bytes(COMPLEX)
    (home / "AGENTS.md").write_bytes(policy)
    unrelated = home / "auth.json"
    unrelated.write_bytes(b"private unrelated data")
    installed = invoke(command, capsys)
    assert (home / "config.toml").read_bytes().startswith(COMPLEX)
    assert (home / "AGENTS.md").read_bytes().startswith(policy)
    assert [Path(p).read_bytes() for p in installed["backups"]] == [COMPLEX, policy]
    if os.name != "nt":
        assert all(Path(p).stat().st_mode & 0o077 == 0 for p in installed["backups"])
    # A real update changes an explicit working directory, not unrelated settings.
    new_cwd = home.parent / "another directory"
    new_cwd.mkdir()
    changed = command.copy()
    changed[changed.index("--cwd") + 1] = str(new_cwd)
    updated = invoke(changed, capsys)
    assert updated["changed_files"] == [str(home / "config.toml")]
    assert invoke(changed, capsys)["changed_files"] == []
    invoke(["codex", "disable"], capsys)
    assert not invoke(["codex", "inspect"], capsys)["managed_registration"]["enabled"]
    assert "single-client" in invoke(["codex", "enable"], capsys, 2)
    invoke(["codex", "enable", "--single-client"], capsys)
    assert invoke(["codex", "inspect"], capsys)["managed_registration"]["enabled"]
    invoke(["codex", "uninstall"], capsys)
    assert (home / "config.toml").read_bytes() == COMPLEX
    assert (home / "AGENTS.md").read_bytes() == policy
    assert unrelated.read_bytes() == b"private unrelated data"


@pytest.mark.parametrize(
    "original, message",
    [
        (b"[invalid", "Malformed TOML"),
        (b'[mcp_servers.agentforge]\ncommand="existing"\n', "Unmanaged agentforge"),
        (b'mcp_servers = {other = {command = "x"}}\n', "Malformed TOML"),
        (integration.CONFIG_END.encode(), "ownership markers"),
    ],
)
def test_conflicts_leave_every_file_untouched(setup, capsys, original, message):
    home, command = setup
    home.mkdir(parents=True)
    (home / "config.toml").write_bytes(original)
    assert message in invoke(command, capsys, 2)
    assert (home / "config.toml").read_bytes() == original
    assert not (home / "AGENTS.md").exists()
    assert not list(home.glob("*.agentforge-backup-*"))


def test_shadowed_policy_and_instruction_limit(setup, capsys):
    home, command = setup
    home.mkdir(parents=True)
    override = home / "AGENTS.override.md"
    override.write_text("Operator override", encoding="utf-8")
    assert "shadows" in invoke(command, capsys, 2)
    assert not (home / "config.toml").exists()
    override.unlink()
    (home / "config.toml").write_text("project_doc_max_bytes = 10\n", encoding="utf-8")
    assert "project_doc_max_bytes" in invoke(command, capsys, 2)
    command.remove("--instructions")
    invoke(command, capsys)
    assert not (home / "AGENTS.md").exists()
    override.write_text("Operator override", encoding="utf-8")
    assert invoke(["codex", "inspect"], capsys)["instructions_shadowed"]


@pytest.mark.parametrize(
    "option",
    ["--python", "--installation", "--database", "--workers", "--cwd", "--coding"],
)
def test_invalid_paths_are_rejected_before_changes(setup, capsys, option):
    home, command = setup
    if option == "--coding":
        command += [option, str(home / "missing")]
    else:
        command[command.index(option) + 1] = str(home / "missing")
    assert "path" in invoke(command, capsys, 2)
    assert not home.exists()


def test_relative_executable_and_wrong_installation(setup, capsys, tmp_path):
    home, command = setup
    relative = command.copy()
    relative[relative.index("--python") + 1] = "python"
    assert "absolute" in invoke(relative, capsys, 2)
    command[command.index("--installation") + 1] = str(tmp_path)
    assert "import AgentForge" in invoke(command, capsys, 2)
    assert not home.exists()


def test_uninitialized_database_and_single_client_ack(setup, capsys):
    home, command = setup
    no_ack = command.copy()
    no_ack.remove("--single-client")
    assert "single-client" in invoke(no_ack, capsys, 2)
    db = Path(command[command.index("--database") + 1])
    db.write_bytes(b"")
    assert "Upgrade this database" in invoke(command, capsys, 2)
    assert not home.exists()


def test_duplicate_database_registration(setup, capsys):
    home, command = setup
    invoke(command, capsys)
    entry = invoke(["codex", "inspect"], capsys)["managed_registration"]
    with (home / "config.toml").open("a", encoding="utf-8") as stream:
        stream.write(
            '\n[mcp_servers.second]\ncommand="python"\nargs='
            + json.dumps(entry["args"])
            + "\n"
        )
    before = (home / "config.toml").read_bytes()
    assert "Another MCP registration" in invoke(command, capsys, 2)
    assert (home / "config.toml").read_bytes() == before


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_managed_block_cannot_capture_unrelated_toml(setup, capsys, operation):
    home, command = setup
    invoke(command, capsys)
    with (home / "config.toml").open("a", encoding="utf-8") as stream:
        stream.write('user_owned = "outside marker but attached to table"\n')
    before = (home / "config.toml").read_bytes()
    assert "boundaries" in invoke(
        command if operation == "install" else ["codex", "uninstall"], capsys, 2
    )
    assert (home / "config.toml").read_bytes() == before


def test_repository_codex_home_is_never_modified(setup, capsys, tmp_path):
    _, command = setup
    repo = tmp_path / "opened-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    instructions = repo / "AGENTS.md"
    instructions.write_bytes(b"User repository rules")
    assert "outside every repository" in invoke(
        command + ["--codex-home", str(repo / ".codex")], capsys, 2
    )
    assert instructions.read_bytes() == b"User repository rules"
    assert not (repo / ".codex").exists()


@pytest.mark.posix
def test_linked_config_is_rejected(setup, capsys, tmp_path):
    home, command = setup
    home.mkdir(parents=True)
    other = tmp_path / "other"
    other.write_bytes(b'model="retain"\n')
    (home / "config.toml").symlink_to(other)
    assert "linked" in invoke(command, capsys, 2)
    assert other.read_bytes() == b'model="retain"\n'


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\Operator Name\AgentForge\venv\Scripts\python.exe",
        r'C:\Users\O"Brien\Workers\config.toml',
        "/Users/operator name/AgentForge/.venv/bin/python",
        "/Users/operator/日本語/$(literal);file",
        "/Users/operator/🧰/python",
    ],
)
def test_windows_and_macos_paths_roundtrip_without_shell_interpolation(path):
    entry = {
        "command": path,
        "args": ["-I", "-m", "agentforge.mcp.server", path],
        "cwd": path,
    }
    text = integration.update_config("", entry)
    assert tomllib.loads(text)["mcp_servers"]["agentforge"] == entry
    assert integration.update_config(text, None) == ""


def test_backup_and_concurrent_change_failures_preserve_original(
    setup, capsys, monkeypatch
):
    home, command = setup
    home.mkdir(parents=True)
    config = home / "config.toml"
    config.write_bytes(COMPLEX)
    real_read = integration.read_private
    reads = 0

    def changed(path):
        nonlocal reads
        value = real_read(path)
        if path == config:
            reads += 1
            if reads == 2:
                return b"concurrent edit"
        return value

    monkeypatch.setattr(integration, "read_private", changed)
    assert "Files changed" in invoke(command, capsys, 2)
    assert config.read_bytes() == COMPLEX
    assert not list(home.glob("*.agentforge-backup-*"))


def test_actual_codex_cli_parses_generated_config_in_disposable_home(
    setup, capsys, tmp_path
):
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("Codex CLI unavailable; hosted matrix does not require a client")
    home, command = setup
    invoke(command, capsys)
    for name in ("unrelated-one", "unrelated-two"):
        repo = tmp_path / name
        repo.mkdir()
        (repo / ".git").mkdir()
        rules = repo / "AGENTS.md"
        rules.write_bytes(b"Original repository development rules")
        result = subprocess.run(
            [codex, "mcp", "get", "agentforge", "--json"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "CODEX_HOME": str(home)},
        )
        assert result.returncode == 0, result.stderr
        actual = json.loads(result.stdout)
        assert actual["name"] == "agentforge"
        assert actual["transport"]["command"] == sys.executable
        assert actual["transport"]["args"][1:3] == ["-m", "agentforge.mcp.server"]
        assert actual["disabled_tools"] == ["cleanup_coding_workspace"]
        assert rules.read_bytes() == b"Original repository development rules"


def test_credentials_are_forwarded_by_name_only(setup, capsys, monkeypatch):
    home, command = setup
    secret = "synthetic-secret-never-copy"
    monkeypatch.setenv("AGENTFORGE_TEST_KEY", secret)
    workers = Path(command[command.index("--workers") + 1])
    workers.write_text(
        """[[providers]]
id = "named"
type = "openai_compatible"
base_url = "https://example.invalid/v1"
api_key_env = "AGENTFORGE_TEST_KEY"
[[workers]]
id = "explicit-cloud"
provider = "named"
model = "synthetic"
""",
        encoding="utf-8",
    )
    invoke(command, capsys)
    entry = invoke(["codex", "inspect"], capsys)["managed_registration"]
    assert entry["env_vars"] == ["AGENTFORGE_TEST_KEY"]
    assert secret not in (home / "config.toml").read_text()
    assert secret not in (home / "AGENTS.md").read_text()


def test_duplicate_database_with_relative_alias(setup, capsys):
    home, command = setup
    home.mkdir(parents=True)
    db = Path(command[command.index("--database") + 1])
    text = (
        '[mcp_servers.other]\ncommand="python"\ncwd='
        + json.dumps(str(db.parent))
        + '\nargs=["--database-url=sqlite:///'
        + db.name
        + '"]\n'
    )
    (home / "config.toml").write_text(text, encoding="utf-8")
    assert "Another MCP registration" in invoke(command, capsys, 2)
    assert (home / "config.toml").read_text() == text
    assert not (home / "AGENTS.md").exists()


def test_python_probe_failure_is_actionable_and_never_echoes_payload(
    setup, capsys, monkeypatch
):
    home, command = setup

    def unavailable(*args, **kwargs):
        assert isinstance(args[0], list)
        assert kwargs.get("shell", False) is False
        raise subprocess.CalledProcessError(1, args[0], stderr="PRIVATE")

    monkeypatch.setattr(integration.subprocess, "run", unavailable)
    output = invoke(command, capsys, 2)
    assert "import AgentForge" in output and "PRIVATE" not in output
    assert not home.exists()


def test_generated_stdio_runs_from_python_and_cwd_with_spaces(setup, capsys, tmp_path):
    import asyncio
    import venv

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home, command = setup
    # A disposable interpreter with the test environment's dependencies; no pip,
    # downloads, real home changes, API use or production venv mutation.
    venv_path = tmp_path / "python environment with spaces"
    builder = venv.EnvBuilder(with_pip=False)
    builder.create(venv_path)
    context = builder.ensure_directories(venv_path)
    site = Path(context.lib_path)
    site.mkdir(parents=True, exist_ok=True)
    (site / "test-dependencies.pth").write_text(
        "\n".join(str(Path(p).resolve()) for p in sys.path if p) + "\n",
        encoding="utf-8",
    )
    command[command.index("--python") + 1] = context.env_exe
    cwd = tmp_path / "working directory with spaces"
    cwd.mkdir()
    command[command.index("--cwd") + 1] = str(cwd)
    invoke(command, capsys)
    entry = invoke(["codex", "inspect"], capsys)["managed_registration"]

    async def execute():
        parameters = StdioServerParameters(
            command=entry["command"], args=entry["args"], cwd=entry["cwd"]
        )
        async with (
            asyncio.timeout(20),
            stdio_client(parameters, errlog=errlog) as (read, write),
        ):
            async with ClientSession(read, write) as client:
                await client.initialize()
                status = await client.call_tool("agentforge_status", {})
                assert status.structuredContent["available"]
                assert status.structuredContent["project_count"] == 0
                assert status.structuredContent["queued_tasks"] == 0
                assert "delegate_task" in {
                    tool.name for tool in (await client.list_tools()).tools
                }

    with (tmp_path / "mcp.stderr").open("w", encoding="utf-8") as errlog:
        asyncio.run(execute())
    assert home.is_dir()


def test_unrelated_mcp_database_syntax_is_preserved(setup, capsys):
    home, command = setup
    home.mkdir(parents=True)
    original = (
        b'[mcp_servers.unrelated]\ncommand="other"\n'
        b'args=["--database-url", "ODBC DSN=unrelated"]\n'
    )
    (home / "config.toml").write_bytes(original)
    invoke(command, capsys)
    invoke(["codex", "uninstall"], capsys)
    assert (home / "config.toml").read_bytes() == original
