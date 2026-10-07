"""Add tagged Windows identity without observing or changing legacy roots.

Revision ID: 0006_windows_root_identity
Revises: 0005_task_telemetry
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_windows_root_identity"
down_revision = "0005_task_telemetry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("root_identity", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Windows-only registrations retain NULL POSIX identity and fail closed.
    op.drop_column("projects", "root_identity")
