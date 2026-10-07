"""Terminal Task telemetry and configured execution target snapshots.

Revision ID: 0005_task_telemetry
Revises: 0004_tasks
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_task_telemetry"
down_revision = "0004_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("provider", sa.Text()))
    op.add_column("tasks", sa.Column("model", sa.Text()))
    op.add_column("tasks", sa.Column("queue_duration_seconds", sa.Float()))
    op.add_column(
        "tasks",
        sa.Column(
            "telemetry_status", sa.String(20), nullable=False, server_default="pending"
        ),
    )
    # Old terminal executions have no target/monotonic observations. No backfill
    # pretends these facts can be reconstructed from today's Worker config.
    op.execute(
        "UPDATE tasks SET telemetry_status = 'unavailable' "
        "WHERE state IN ('completed', 'failed', 'cancelled')"
    )
    op.create_table(
        "task_telemetry",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("error_category", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        *[
            sa.Column(name, sa.Float())
            for name in (
                "queue_duration_seconds",
                "execution_duration_seconds",
                "total_duration_seconds",
                "model_request_duration_seconds",
                "backend_total_duration_seconds",
                "model_load_duration_seconds",
                "prompt_evaluation_duration_seconds",
                "generation_duration_seconds",
                "ttft_seconds",
                "tokens_per_second",
                "total_tool_duration_seconds",
            )
        ],
        *[
            sa.Column(name, sa.Integer())
            for name in (
                "model_call_count",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "observed_prompt_tokens",
                "observed_completion_tokens",
                "tool_call_count",
                "tool_output_bytes",
            )
        ],
        sa.Column("prompt_observed_turns", sa.Integer(), nullable=False),
        sa.Column("completion_observed_turns", sa.Integer(), nullable=False),
        sa.Column("token_usage_complete", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("task_id"),
        sa.CheckConstraint(
            "state IN ('completed', 'failed', 'cancelled')",
            name="ck_task_telemetry_state",
        ),
    )
    op.create_index(
        "ix_task_telemetry_created", "task_telemetry", ["created_at", "task_id"]
    )
    for field in ("project_id", "agent_id", "worker_id", "provider", "model", "state"):
        label = field.removesuffix("_id")
        op.create_index(
            f"ix_task_telemetry_{label}_created",
            "task_telemetry",
            [field, "created_at", "task_id"],
        )


def downgrade() -> None:
    op.drop_table("task_telemetry")
    op.drop_column("tasks", "telemetry_status")
    op.drop_column("tasks", "queue_duration_seconds")
    op.drop_column("tasks", "model")
    op.drop_column("tasks", "provider")
