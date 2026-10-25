"""Short atomic connection admission transactions; no scheduler or quota guesses."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import SQLAlchemyError

from agentforge.core.policy import PolicyDenied, PolicyStorageError
from agentforge.db.models import ConnectionResourceRecord


def denial(record, budget, now):
    until = record.cooldown_until
    if until and until.replace(tzinfo=UTC) > now:
        return "cooldown"
    if budget.max_requests is not None and record.requests_used >= budget.max_requests:
        return "request_budget"
    if budget.max_tokens is not None:
        if record.usage_unknown:
            return "token_usage_unknown"
        if record.in_flight:
            return "token_budget_in_flight"
        if record.tokens_used >= budget.max_tokens:
            return "token_budget"
    return None


class ResourceRepository:
    def __init__(self, sessions):
        self._sessions = sessions

    def get(self, identity):
        try:
            with self._sessions() as session:
                row = session.get(ConnectionResourceRecord, identity)
                if row is None:
                    return None
                session.expunge(row)
                if row.observed_at:
                    row.observed_at = row.observed_at.replace(tzinfo=UTC)
                if row.cooldown_until:
                    row.cooldown_until = row.cooldown_until.replace(tzinfo=UTC)
                return row
        except SQLAlchemyError:
            raise PolicyStorageError(
                "Could not read resource policy evidence"
            ) from None

    def admit(self, identity, budget):
        try:
            with self._sessions() as session:
                # Acquires SQLite's writer slot BEFORE reading counters. Independent
                # callers cannot both admit from the same pre-increment value.
                session.execute(
                    insert(ConnectionResourceRecord)
                    .values(connection_id=identity)
                    .on_conflict_do_nothing()
                )
                row = session.scalar(
                    select(ConnectionResourceRecord).where(
                        ConnectionResourceRecord.connection_id == identity
                    )
                )
                if code := denial(row, budget, datetime.now(UTC)):
                    raise PolicyDenied(code)
                remaining_tokens = (
                    budget.max_tokens - row.tokens_used
                    if budget.max_tokens is not None
                    else None
                )
                row.requests_used += 1
                row.in_flight += 1
                self._commit(session)
                return remaining_tokens
        except SQLAlchemyError:
            raise PolicyStorageError(
                "Could not reserve resource policy budget"
            ) from None

    @staticmethod
    def _commit(session):
        try:
            session.commit()
        except SQLAlchemyError:
            session.invalidate()
            raise

    def finish(self, identity, *, tokens, error_code, retry_after):
        try:
            with self._sessions() as session:
                # Serialize updates before reading; same rule as admission.
                session.execute(
                    insert(ConnectionResourceRecord)
                    .values(connection_id=identity)
                    .on_conflict_do_nothing()
                )
                row = session.get(ConnectionResourceRecord, identity)
                row.in_flight = max(0, row.in_flight - 1)
                if tokens is None:
                    row.usage_unknown = True
                elif (
                    tokens > 1_000_000_000_000
                    or row.tokens_used + tokens > 1_000_000_000_000
                ):
                    row.usage_unknown = True
                else:
                    row.tokens_used += tokens
                now = datetime.now(UTC)
                row.observed_at = now
                row.last_error = error_code
                if error_code == "rate_limited" and retry_after is not None:
                    until = now + timedelta(seconds=retry_after)
                    if (
                        row.cooldown_until is None
                        or row.cooldown_until.replace(tzinfo=UTC) < until
                    ):
                        row.cooldown_until = until
                self._commit(session)
        except SQLAlchemyError:
            raise PolicyStorageError(
                "Could not record resource policy evidence"
            ) from None
