"""Durable Council identity and ordered membership, without execution copies.

Revision ID: 0007_councils
Revises: 0006_windows_root_identity
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_councils"
down_revision = "0006_windows_root_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "councils",
        sa.Column("council_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_councils_created", "councils", ["created_at", "council_id"])
    op.create_table(
        "council_participants",
        sa.Column("council_id", sa.Uuid(), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["council_id"], ["councils.council_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
        sa.UniqueConstraint("council_id", "worker_id", name="uq_council_worker"),
        sa.UniqueConstraint("task_id", name="uq_council_task"),
        sa.CheckConstraint("ordinal >= 0 AND ordinal < 16", name="ck_council_ordinal"),
    )


def downgrade() -> None:
    op.drop_table("council_participants")
    op.drop_index("ix_councils_created", table_name="councils")
    op.drop_table("councils")
