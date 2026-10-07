"""Offline operator setup, administration, diagnostics and trust boundaries."""

import io
import json
import shlex
import shutil
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command

from agentforge.cli import main
from agentforge.db.database import create_session_factory
from agentforge.db.migrate import UnsupportedSchema, _configuration, upgrade_database
from agentforge.db.tasks import TaskRepository
from agentforge.providers.ollama import OllamaProvider

SECRET = "PRIVATE_OPERATOR_TEST_SECRET"
ROOT = Path(__file__).parents[1]


def invoke(capsys, *args, expected=0):
    assert main(list(map(str, args))) == expected
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "Traceback" not in output.err and "SELECT " not in output.err
    return json.loads(output.out) if output.out else output.err


@pytest.mark.parametrize(
    "args",
    [[], ["project"], ["worker", "check"], ["project", "inspect", SECRET], [SECRET]],
)
def test_invalid_arguments_are_safe(capsys, args):
    assert "invalid_arguments" in invoke(capsys, *args, expected=2)


@pytest.mark.parametrize(
    "args",
    [[], ["db"], ["project"], ["worker"], ["agent"], ["coding"], ["mcp"], ["web"]],
)
def test_discoverable_help(capsys, args):
    with pytest.raises(SystemExit) as result:
        main([*args, "--help"])
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_fresh_database_and_readonly_status(tmp_path, capsys):
    path = tmp_path / "fresh %# space.db"
    url = "sqlite:///" + path.as_posix()
    assert invoke(capsys, "db", "status", "--database-url", url, expected=4) == {
        "state": "uninitialized"
    }
    assert not path.exists()
    invoke(capsys, "project", "list", "--database-url", url, expected=4)
    assert not path.exists()
    assert invoke(capsys, "db", "upgrade", "--database-url", url)["state"] == "current"
    before = path.read_bytes()
    assert invoke(capsys, "db", "status", "--database-url", url)["state"] == "current"
    assert path.read_bytes() == before
    invoke(capsys, "db", "upgrade", "--database-url", url)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "kind", ["empty", "foreign", "view", "old_revision", "corrupt"]
)
def test_database_states_do_not_adopt_unknown_storage(tmp_path, capsys, kind):
    path = tmp_path / "unknown.db"
    path.touch()
    if kind == "corrupt":
        path.write_bytes(SECRET.encode())
    else:
        with sqlite3.connect(path) as connection:
            if kind == "foreign":
                connection.execute("CREATE TABLE unrelated (value TEXT)")
                connection.execute("INSERT INTO unrelated VALUES (?)", (SECRET,))
            if kind == "view":
                connection.execute("CREATE VIEW unrelated AS SELECT 1")
            if kind == "old_revision":
                connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
                connection.execute("INSERT INTO alembic_version VALUES ('0008_coding')")
    before = path.read_bytes()
    expected = (
        "uninitialized"
        if kind == "empty"
        else "inaccessible"
        if kind == "corrupt"
        else "unsupported"
    )
    assert (
        invoke(
            capsys,
            "db",
            "status",
            "--database-url",
            "sqlite:///" + path.as_posix(),
            expected=3 if kind == "corrupt" else 4,
        )["state"]
        == expected
    )
    assert path.read_bytes() == before
    if kind != "empty":
        invoke(
            capsys,
            "db",
            "upgrade",
            "--database-url",
            "sqlite:///" + path.as_posix(),
            expected=3 if kind == "corrupt" else 4,
        )
        assert path.read_bytes() == before


def test_database_inaccessible_and_bad_url(tmp_path, capsys):
    url = "sqlite:///" + (tmp_path / "missing" / "db").as_posix()
    assert (
        invoke(capsys, "db", "status", "--database-url", url, expected=3)["state"]
        == "inaccessible"
    )
    invoke(capsys, "db", "upgrade", "--database-url", url, expected=3)
    for url in [
        SECRET,
        "sqlite:///:memory:",
        "postgresql://user:" + SECRET + "@host/db",
        "sqlite:///db?mode=ro",
        "sqlite:///db?unrecognized",
    ]:
        invoke(capsys, "db", "status", "--database-url", url, expected=2)


def test_legacy_and_alembic_upgrade_reject_unstamped_tables(tmp_path):
    url = "sqlite:///" + (tmp_path / "foreign.db").as_posix()
    with sqlite3.connect(tmp_path / "foreign.db") as connection:
        connection.execute("CREATE TABLE foreign_data (value TEXT)")
    with pytest.raises(UnsupportedSchema):
        upgrade_database(url)
    from agentforge.db.database import create_database_engine

    engine = create_database_engine(url)
    try:
        config = _configuration()
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(UnsupportedSchema):
                command.upgrade(config, "head")
    finally:
        engine.dispose()


def test_project_lifecycle_and_retained_history(
    database, tmp_path, capsys, monkeypatch
):
    engine, url = database
    root = tmp_path / "Project with spaces"
    root.mkdir()
    (root / "source.py").write_text("def example(): return 1\n")
    (root / "child").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "agentforge.tasks.engine.TaskEngine.start",
        lambda *_: pytest.fail("Admin must not start TaskEngine"),
    )
    project = invoke(
        capsys, "project", "add", root.name, "--name", "Example", "--database-url", url
    )["registration"]
    identity = UUID(project["project_id"])
    assert project["root_path"] == str(root)
    assert "root_identity" not in project
    listing = invoke(capsys, "project", "list", "--database-url", url)
    assert len(listing["projects"]) == 1  # No recursive registration.
    assert listing["projects"][0]["indexed_at"] is None
    invoke(
        capsys,
        "project",
        "add",
        root / "child" / "..",
        "--name",
        "Again",
        "--database-url",
        url,
        expected=5,
    )
    inspection = invoke(capsys, "project", "inspect", identity, "--database-url", url)
    assert inspection["registration"] == project
    assert inspection["live_git"]["status"] == "not_repository"
    indexed = invoke(capsys, "project", "index", identity, "--database-url", url)
    assert indexed["file_count"] == indexed["symbol_count"] == 1
    assert indexed["parse_failure_count"] == 0
    listing = invoke(capsys, "project", "list", "--database-url", url)
    assert listing["projects"][0]["indexed_at"] == indexed["indexed_at"]
    tasks = TaskRepository(create_session_factory(engine))
    council, members = tasks.add_council(
        project_id=identity,
        agent_id="repo_explorer",
        request="History",
        targets=(("one", "ollama", "model"), ("two", "ollama", "model")),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("remove\n"))
    invoke(capsys, "project", "remove", identity, "--database-url", url, expected=2)
    invoke(
        capsys, "project", "remove", identity, "--database-url", url, "--y", expected=2
    )
    assert invoke(capsys, "project", "list", "--database-url", url)["projects"]
    invoke(capsys, "project", "remove", identity, "--database-url", url, "--yes")
    assert invoke(capsys, "project", "list", "--database-url", url)["projects"] == []
    assert (root / "source.py").exists()
    for member in members:
        assert tasks.get(member.task_id).project_id == identity
    from agentforge.db.councils import CouncilRepository

    assert CouncilRepository(create_session_factory(engine)).get(council.council_id)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT count(*) FROM project_indexes").scalar()
            == 0
        )


@pytest.mark.parametrize("answer", ["", "yes\n", "remove\n"])
def test_terminal_removal_confirmation(database, tmp_path, capsys, monkeypatch, answer):
    project = invoke(
        capsys,
        "project",
        "add",
        tmp_path,
        "--name",
        "Terminal",
        "--database-url",
        database[1],
    )["registration"]
    stream = io.StringIO(answer)
    stream.isatty = lambda: True
    monkeypatch.setattr("sys.stdin", stream)
    invoke(
        capsys,
        "project",
        "remove",
        project["project_id"],
        "--database-url",
        database[1],
        expected=0 if answer == "remove\n" else 2,
    )
    remaining = invoke(capsys, "project", "list", "--database-url", database[1])[
        "projects"
    ]
    assert bool(remaining) == (answer != "remove\n")


def test_project_missing_invalid_and_bounded_lists(database, tmp_path, capsys):
    url = database[1]
    invoke(
        capsys,
        "project",
        "add",
        tmp_path / "missing",
        "--name",
        "Bad",
        "--database-url",
        url,
        expected=6,
    )
    file = tmp_path / "file"
    file.touch()
    invoke(
        capsys,
        "project",
        "add",
        file,
        "--name",
        "Bad",
        "--database-url",
        url,
        expected=6,
    )
    invoke(
        capsys,
        "project",
        "add",
        tmp_path,
        "--name",
        " ",
        "--database-url",
        url,
        expected=2,
    )
    for operation in ["inspect", "index", "remove"]:
        invoke(capsys, "project", operation, uuid4(), "--database-url", url, expected=5)
    for number in range(3):
        root = tmp_path / str(number)
        root.mkdir()
        invoke(
            capsys, "project", "add", root, "--name", str(number), "--database-url", url
        )
    first = invoke(capsys, "project", "list", "--database-url", url, "--limit", 2)
    assert len(first["projects"]) == 2 and first["next_offset"] == 2
    final = invoke(
        capsys, "project", "list", "--database-url", url, "--limit", 2, "--offset", 2
    )
    assert len(final["projects"]) == 1 and final["next_offset"] is None
    invoke(capsys, "project", "list", "--database-url", url, "--limit", 101, expected=2)


@pytest.mark.posix
def test_replaced_root_cached_list_and_safe_inspection(database, tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    identity = invoke(
        capsys, "project", "add", root, "--name", "Root", "--database-url", database[1]
    )["registration"]["project_id"]
    root.rename(tmp_path / "old")
    root.mkdir()
    assert invoke(capsys, "project", "list", "--database-url", database[1])["projects"]
    invoke(
        capsys,
        "project",
        "inspect",
        identity,
        "--database-url",
        database[1],
        expected=6,
    )
    invoke(
        capsys, "project", "index", identity, "--database-url", database[1], expected=6
    )


@pytest.mark.parametrize(
    "example", ["workers.example.toml", "workers.providers.example.toml"]
)
def test_worker_examples_without_probing(capsys, monkeypatch, example):
    async def health(*_):
        pytest.fail("Configuration operations must not probe")

    monkeypatch.setattr(OllamaProvider, "health", health)
    config = ROOT / "config" / example
    assert (
        invoke(capsys, "worker", "config-check", "--workers", config)["configuration"]
        == "valid"
    )
    listing = invoke(capsys, "worker", "list", "--workers", config)
    assert listing["availability"] == "not_probed"
    assert "endpoint" not in json.dumps(listing)
    invoke(capsys, "worker", "check", "missing", "--workers", config, expected=5)


@pytest.mark.parametrize(
    "response,expected,status",
    [
        (
            httpx.Response(200, json={"models": [{"name": "gpt-oss:20b"}]}),
            0,
            "available",
        ),
        (httpx.Response(200, json={"models": []}), 7, "unavailable"),
        (httpx.Response(503, text=SECRET), 7, "unavailable"),
        (httpx.Response(200, text=SECRET), 7, "unavailable"),
    ],
)
def test_explicit_ollama_health_uses_contract(
    capsys, monkeypatch, response, expected, status
):
    seen = []

    def request(value):
        seen.append(value)
        return response

    monkeypatch.setattr(
        "agentforge.providers.factory.PROVIDER_TYPES",
        {"ollama": OllamaProvider, "openai_compatible": object},
    )
    monkeypatch.setattr(
        "agentforge.cli.create_providers",
        lambda _: {"ollama": OllamaProvider(transport=httpx.MockTransport(request))},
    )
    result = invoke(
        capsys,
        "worker",
        "check",
        "local-4080",
        "--workers",
        ROOT / "config/workers.example.toml",
        expected=expected,
    )
    assert result["status"] == status
    assert (
        len(seen) == 1 and seen[0].method == "GET" and seen[0].url.path == "/api/tags"
    )


def test_compatible_not_probed_and_credentials_safe(tmp_path, capsys, monkeypatch):
    config = tmp_path / "workers.toml"
    config.write_text(
        '[[providers]]\nid="remote"\ntype="openai_compatible"\nbase_url="https://remote.invalid/private/v1"\napi_key_env="TEST_OPERATOR_TOKEN"\n'
        '[[workers]]\nid="worker"\nprovider="remote"\nmodel="configured"\n[workers.options]\nstop="'
        + SECRET
        + '"\n'
    )
    invoke(capsys, "worker", "config-check", "--workers", config, expected=2)
    monkeypatch.setenv("TEST_OPERATOR_TOKEN", SECRET)
    invoke(capsys, "worker", "config-check", "--workers", config)
    result = invoke(capsys, "worker", "check", "worker", "--workers", config)
    assert result["status"] == "not_probed"
    assert result["backend_available"] is result["model_available"] is None
    invoke(capsys, "worker", "list", "--workers", config)
    config.write_text(
        config.read_text().replace(
            "https://remote.invalid/private/v1",
            "https://user:" + SECRET + "@remote.invalid",
        )
    )
    invoke(capsys, "worker", "list", "--workers", config, expected=2)
    config.write_text('bad = "' + SECRET + '"\nworkers = [')
    invoke(capsys, "worker", "config-check", "--workers", config, expected=2)
    invoke(
        capsys, "worker", "config-check", "--workers", tmp_path / "missing", expected=2
    )


def test_health_exception_safe_and_provider_closed(capsys, monkeypatch):
    class Provider:
        closed = False

        async def health(self, _):
            raise RuntimeError(SECRET)

        async def aclose(self):
            self.closed = True

    provider = Provider()
    monkeypatch.setattr(
        "agentforge.cli.create_providers", lambda _: {"ollama": provider}
    )
    invoke(
        capsys,
        "worker",
        "check",
        "local-4080",
        "--workers",
        ROOT / "config/workers.example.toml",
        expected=7,
    )
    assert provider.closed


@pytest.mark.posix
def test_coding_configuration_and_host_checks(database, tmp_path, capsys):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    git = Path(shutil.which("git")).resolve()
    validator = tmp_path / "trusted-validator"
    shutil.copyfile(git, validator)
    validator.chmod(0o700)
    config = tmp_path / "coding.toml"
    config.write_text(
        "workspace_parent="
        + json.dumps(str(parent))
        + "\ngit_executable="
        + json.dumps(str(git))
        + "\n[validations.check]\nargv=["
        + json.dumps(str(validator))
        + ',"'
        + SECRET
        + '"]\n'
    )
    show = invoke(capsys, "coding", "show", "--coding", config)
    assert show["validations"]["check"]["argument_count"] == 2
    assert (
        invoke(
            capsys, "coding", "check", "--coding", config, "--database-url", database[1]
        )["commands_executed"]
        is False
    )
    enabled = invoke(capsys, "agent", "list", "--coding", config)["agents"]
    assert {a["id"] for a in enabled} == {"repo_explorer", "coder"}
    assert all("system_prompt" not in a for a in enabled)
    assert [a["id"] for a in invoke(capsys, "agent", "list")["agents"]] == [
        "repo_explorer"
    ]
    parent.chmod(0o755)
    invoke(
        capsys,
        "coding",
        "check",
        "--coding",
        config,
        "--database-url",
        database[1],
        expected=2,
    )
    parent.chmod(0o700)
    invoke(
        capsys,
        "project",
        "add",
        parent,
        "--name",
        "Overlap",
        "--database-url",
        database[1],
    )
    invoke(
        capsys,
        "coding",
        "check",
        "--coding",
        config,
        "--database-url",
        database[1],
        expected=2,
    )
    config.write_text('workspace_parent="relative"\ngit_executable="' + SECRET + '"\n')
    invoke(capsys, "coding", "show", "--coding", config, expected=2)


def test_service_wrappers_reuse_existing_lifecycle(capsys, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "agentforge.mcp.server.run", lambda args: seen.append(args) or 0
    )
    monkeypatch.setattr(
        "agentforge.web.server.run", lambda args: seen.append(args) or 0
    )
    for service in ["mcp", "web"]:
        invoke(
            capsys,
            service,
            "--database-url",
            "sqlite:///explicit.db",
            "--workers",
            "explicit.toml",
        )
    assert [a.database_url for a in seen] == ["sqlite:///explicit.db"] * 2
    assert [a.concurrency for a in seen] == [1, 1]


def test_readme_setup_commands_and_minimal_toml(tmp_path, capsys):
    from agentforge.cli import parser

    readme = (ROOT / "README.md").read_text()
    config = tmp_path / "workers.toml"
    config.write_text(readme.split("```toml\n", 1)[1].split("```", 1)[0])
    invoke(capsys, "worker", "config-check", "--workers", config)
    for line in readme.splitlines():
        if line.startswith("agentforge "):
            args = shlex.split(line.replace("<PROJECT_UUID>", str(uuid4())))
            parser().parse_args(args[1:])
    assert "```python" not in readme


def test_factory_binding_conflict_is_invalid_config(tmp_path, capsys):
    config = tmp_path / "workers.toml"
    config.write_text(
        '[[providers]]\nid="ollama"\ntype="ollama"\n'
        'base_url="http://named.invalid"\n'
        '[[workers]]\nid="inline"\nprovider="ollama"\nmodel="model"\n'
        'endpoint="http://inline.invalid"\n'
    )
    for operation in ("list", "config-check"):
        invoke(capsys, "worker", operation, "--workers", config, expected=2)


@pytest.mark.posix
@pytest.mark.parametrize("problem", ["missing_git", "missing_validator", "symlink_git"])
def test_coding_host_executable_problems(database, tmp_path, capsys, problem):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    executable = tmp_path / "git"
    if problem == "symlink_git":
        executable.symlink_to(Path(shutil.which("git")).resolve())
    elif problem != "missing_git":
        shutil.copyfile(shutil.which("git"), executable)
        executable.chmod(0o700)
    config = tmp_path / "coding.toml"
    config.write_text(
        "workspace_parent=" + json.dumps(str(parent)) + "\n"
        "git_executable=" + json.dumps(str(executable)) + "\n"
        "[validations.check]\nargv=[" + json.dumps(str(tmp_path / "missing")) + "]\n"
    )
    invoke(
        capsys,
        "coding",
        "check",
        "--coding",
        config,
        "--database-url",
        database[1],
        expected=2,
    )


def test_live_inspection_and_cached_list_boundary(
    database, tmp_path, capsys, monkeypatch
):
    project = invoke(
        capsys,
        "project",
        "add",
        tmp_path,
        "--name",
        "Registered",
        "--database-url",
        database[1],
    )["registration"]

    def forbidden(*_):
        pytest.fail("Cached list must not inspect roots or Git")

    monkeypatch.setattr(
        "agentforge.projects.service.ProjectRegistry.inspect_project", forbidden
    )
    assert (
        invoke(capsys, "project", "list", "--database-url", database[1])["projects"][0][
            "project_id"
        ]
        == project["project_id"]
    )


@pytest.mark.posix
def test_validator_keeps_strict_policy_when_it_uses_git_executable(
    tmp_path, monkeypatch
):
    from agentforge.coding.config import CodingConfig, ValidationCommand
    from agentforge.coding.filesystem import CodingDirectory
    from agentforge.coding.inspection import check_coding_host
    from agentforge.coding.models import CodingError

    class Backend:
        def observe_root(self, root):
            return None

        def anchored_root(self, *args):
            return nullcontext(CodingDirectory(0, self))

        def regular_file(self, *args):
            raise CodingError("Validator installation is unsafe")

    executable = tmp_path / "git"
    config = CodingConfig(
        workspace_parent=tmp_path / "private",
        git_executable=executable,
        validations={"check": ValidationCommand(argv=(str(executable),))},
    )
    manager = SimpleNamespace(
        config=config, filesystem=Backend(), parent=config.workspace_parent
    )
    monkeypatch.setattr(
        "agentforge.coding.inspection.posix.regular_file", lambda *_: nullcontext()
    )
    monkeypatch.setattr("agentforge.coding.inspection.os.access", lambda *_: True)
    with pytest.raises(CodingError, match="Validator installation is unsafe"):
        check_coding_host(manager)
