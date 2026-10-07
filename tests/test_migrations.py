"""Exercise shipped migrations, every supported downgrade and fresh upgrades offline."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from agentforge.db.database import create_database_engine
from agentforge.db.migrate import upgrade_database


def configuration():
    config = Config()
    from agentforge.db import migrate

    config.set_main_option(
        "script_location", str(Path(migrate.__file__).with_name("migrations"))
    )
    return config


def test_explicit_upgrade_is_idempotent(tmp_path):
    url = "sqlite:///" + (tmp_path / "fresh.db").as_posix()
    upgrade_database(url)
    upgrade_database(url)
    engine = create_database_engine(url)
    try:
        with engine.begin() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == (
                "0008_coding_workspaces"
            )
            config = configuration()
            config.attributes["connection"] = connection
            command.check(config)
    finally:
        engine.dispose()


@pytest.mark.parametrize("revision", ["base", *[f"000{i}" for i in range(1, 9)]])
def test_every_revision_downgrades_and_returns_to_matching_head(database, revision):
    engine, _ = database
    config = configuration()
    scripts = ScriptDirectory.from_config(config)
    chain = list(scripts.walk_revisions())
    assert len(chain) == 8 and scripts.get_heads() == ["0008_coding_workspaces"]
    target = (
        "base"
        if revision == "base"
        else next(r.revision for r in chain if r.revision.startswith(revision + "_"))
    )
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, target)
        command.upgrade(config, "head")
        command.check(config)
