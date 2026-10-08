"""Packaged schema revision checks and explicit database upgrades."""

import argparse
from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from agentforge.db.database import create_database_engine

DatabaseState = Literal["current", "uninitialized", "unsupported", "out_of_date"]


class UnsupportedSchema(ValueError):
    """Unknown storage must never be stamped or adopted."""


def connection_status(connection) -> DatabaseState:
    scripts = ScriptDirectory.from_config(_configuration())
    actual = MigrationContext.configure(connection).get_current_heads()
    if actual == tuple(scripts.get_heads()):
        return "current"
    if not actual:
        inspector = inspect(connection)
        if set(inspector.get_table_names()) - {"alembic_version"} or (
            inspector.get_view_names()
        ):
            return "unsupported"
        return "uninitialized"
    known = {revision.revision for revision in scripts.walk_revisions()}
    return "out_of_date" if len(actual) == 1 and actual[0] in known else "unsupported"


def operator_database_url(database_url: str):
    """Operator commands support explicit persistent local SQLite storage."""
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise ValueError("Invalid database URL") from None
    if (
        url.drivername not in {"sqlite", "sqlite+pysqlite"}
        or not url.database
        or url.database == ":memory:"
        or url.query
        or url.host
        or url.username
        or url.password
        or (isinstance(database_url, str) and "?" in database_url)
    ):
        raise ValueError("Use a persistent local SQLite URL without query options")
    return url


def database_status(database_url: str) -> DatabaseState:
    """Read SQLite through mode=ro; a missing file is never created."""
    url = operator_database_url(database_url)
    path = Path(url.database).absolute()
    try:
        path.stat()
    except FileNotFoundError:
        # A missing parent is an access/setup problem, not an empty database.
        if not path.parent.is_dir():
            raise OSError("Database parent is unavailable") from None
        return "uninitialized"
    readonly = url.set(database=path.as_uri(), query={"mode": "ro", "uri": "true"})
    engine = create_database_engine(readonly)
    try:
        with engine.connect() as connection:
            return connection_status(connection)
    finally:
        engine.dispose()


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
            if connection_status(connection) == "unsupported":
                raise UnsupportedSchema("Database schema is unsupported")
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
