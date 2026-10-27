"""Bounded workflow history and optional per-Task runtime restrictions."""

import sqlalchemy as sa
from alembic import op

revision = "0005_workflows"
down_revision = "0004_specialists"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("execution_limits", sa.JSON(), nullable=True))
    op.create_table(
        "workflows",
        sa.Column("workflow_id", sa.Uuid(), primary_key=True),
        sa.Column("owner_scope", sa.String(110), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_scope", "idempotency_key", name="uq_workflow_key"),
    )
    op.create_index("ix_workflow_state", "workflows", ["state", "created_at"])


def downgrade():
    op.drop_table("workflows")
    op.drop_column("tasks", "execution_limits")
