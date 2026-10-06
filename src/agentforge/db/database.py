"""Explicit SQLAlchemy engine/session setup; schema changes belong to Alembic."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def create_database_engine(url: str | URL) -> Engine:
    """Create an engine without opening a database or creating any tables."""
    return create_engine(url)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
