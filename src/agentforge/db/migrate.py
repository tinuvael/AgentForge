"""Explicit database upgrade entry point, usable from a checkout or installed wheel."""

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

from agentforge.db.database import create_database_engine


def upgrade_database(database_url: str) -> None:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).with_name("migrations"))
    )
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
