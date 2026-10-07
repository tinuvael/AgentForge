"""Alembic CLI wiring; callers may supply an explicit Connection for tests."""

from alembic import context

from agentforge.db.database import Base, create_database_engine
from agentforge.db.migrate import UnsupportedSchema, connection_status
from agentforge.db.models import ProjectRecord  # noqa: F401 -- registers metadata

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection) -> None:
    if connection_status(connection) == "unsupported":
        raise UnsupportedSchema("Database schema is unsupported")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        migrate(supplied_connection)
        return
    # Use runtime SQLite FK and transaction policy for CLI migrations too.
    engine = create_database_engine(config.get_main_option("sqlalchemy.url"))
    try:
        with engine.begin() as connection:
            migrate(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
