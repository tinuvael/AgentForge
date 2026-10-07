"""Packaged schema revision checks and explicit database upgrades."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from agentforge.db.database import create_database_engine


def _configuration() -> Config:
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).with_name("migrations")).replace("%", "%%"),
    )
    return config


def schema_is_current(connection) -> bool:
    """Read the revision stamp without creating tables or changing history."""
    expected = ScriptDirectory.from_config(_configuration()).get_heads()
    actual = MigrationContext.configure(connection).get_current_heads()
    return len(expected) == 1 and actual == (expected[0],)


def upgrade_database(database_url: str) -> None:
    config = _configuration()
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly upgrade an AgentForge database"
    )
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    try:
        upgrade_database(args.database_url)
    except Exception:
        parser.exit(1, "Database upgrade failed; check configuration and access.\n")
    print("Database upgraded to head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
