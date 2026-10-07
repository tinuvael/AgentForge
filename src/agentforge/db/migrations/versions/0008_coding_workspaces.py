"""Durable coding workspace ownership and bounded observations.

Revision ID: 0008_coding_workspaces
Revises: 0007_councils
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_coding_workspaces"
down_revision = "0007_councils"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coding_workspaces",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("branch_name", sa.Text(), nullable=False),
        sa.Column("base_commit", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("repository_path", sa.Text(), nullable=False),
        sa.Column("worktree_path", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("identities", sa.JSON(), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.UniqueConstraint("branch_name", "repository_path", name="uq_coding_branch"),
    )


def downgrade() -> None:
    op.drop_table("coding_workspaces")
