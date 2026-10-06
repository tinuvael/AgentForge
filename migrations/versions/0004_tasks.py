"""Durable explicitly bound Tasks and sanitized runtime history.

Revision ID: 0004_tasks
Revises: 0003_project_root_identity
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_tasks"
down_revision = "0003_project_root_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("reason", sa.String(40)),
        sa.Column("error_code", sa.String(40)),
        sa.Column("execution_result", sa.JSON(none_as_null=True)),
        sa.PrimaryKeyConstraint("task_id"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_state",
        ),
    )
    for name, columns in (
        ("ix_tasks_created", ["created_at", "task_id"]),
        ("ix_tasks_state_created", ["state", "created_at", "task_id"]),
        ("ix_tasks_project", ["project_id"]),
        ("ix_tasks_agent", ["agent_id"]),
        ("ix_tasks_worker", ["worker_id"]),
    ):
        op.create_index(name, "tasks", columns)


def downgrade() -> None:
    op.drop_table("tasks")
