"""Explicit SQLAlchemy engine/session setup; schema changes belong to Alembic."""

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database_engine(url: str | URL) -> Engine:
    """Create an engine without opening a database or creating any tables."""
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
