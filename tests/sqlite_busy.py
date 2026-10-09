"""Real rollback-journal SQLite commit contention, without mocked commits."""

import sqlite3
from contextlib import contextmanager

from sqlalchemy import event


@contextmanager
def blocked_commit(engine):
    # A shared reader lock allows writes/flushes but prevents COMMIT. Use the
    # production journal mode; changing to WAL would hide this failure.
    failures = []
    # Remove unrelated idle connections so the next checkout necessarily
    # encounters the connection used for this rejected commit.
    engine.dispose()

    def error(context):
        failures.append(context)

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "delete"
        connection.exec_driver_sql("PRAGMA busy_timeout=10")
    event.listen(engine, "handle_error", error)
    reader = sqlite3.connect(engine.url.database)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM tasks").fetchone()
        yield
        assert len(failures) == 1
        assert failures[0].statement is None  # COMMIT, not a failed write/flush.
        assert failures[0].original_exception.sqlite_errorcode == sqlite3.SQLITE_BUSY
    finally:
        reader.close()
        event.remove(engine, "handle_error", error)


def durable_tasks(engine):
    # Always inspect through an independent DBAPI connection, never the pool
    # whose failed transaction is under test.
    with sqlite3.connect(engine.url.database) as reader:
        return reader.execute(
            "SELECT task_id, state, cancellation_requested_at, execution_result "
            "FROM tasks ORDER BY task_id"
        ).fetchall()
