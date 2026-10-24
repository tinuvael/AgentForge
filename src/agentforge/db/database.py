"""Explicit SQLAlchemy engine/session setup; schema changes belong to Alembic."""

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database_engine(url: str | URL) -> Engine:
    """Create an engine without opening a database or creating any tables."""
    url = make_url(url)
    if (
        url.get_backend_name() == "sqlite"
        and url.database not in {None, "", ":memory:"}
        and not url.query
    ):
        # Fix relative paths at construction; SQLite and ownership use one spelling.
        url = url.set(database=str(Path(url.database).resolve()))
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.autocommit = True
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
            # Python 3.12 transaction control includes SELECTs, unlike SQLite's
            # legacy mode. Multi-query index reads and refresh hash comparisons
            # therefore share a real transaction, even before their first write.
            connection.autocommit = False

    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
