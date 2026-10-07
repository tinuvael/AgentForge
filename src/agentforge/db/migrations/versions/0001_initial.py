"""Initial supported AgentForge schema.

Revision ID: 0001_initial
Revises: none
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Registration authorizes a recorded platform-tagged root, never a pathname
    # observed later. Index data belongs to that registration and cascades away.
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("root_identity", sa.JSON(none_as_null=True), nullable=False),
        sa.UniqueConstraint("root_path", name="uq_projects_root_path"),
    )
    op.create_table(
        "project_indexes",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_head", sa.String(40), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "indexed_files",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("relative_path", sa.Text(), primary_key=True),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("module_name", sa.Text(), nullable=False),
        sa.Column("observed_hash", sa.String(64), nullable=False),
        sa.Column("parsed_hash", sa.String(64), nullable=True),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "index_symbols",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("parent_id", sa.Text(), nullable=True),
        sa.Column("import_candidate", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "relative_path"],
            ["indexed_files.project_id", "indexed_files.relative_path"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_index_symbols_name", "index_symbols", ["project_id", "name"])
    op.create_table(
        "index_relationships",
        sa.Column("project_id", sa.Uuid(), primary_key=True),
        sa.Column("relative_path", sa.Text(), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("target_text", sa.Text(), nullable=False),
        sa.Column("target_key", sa.Text(), nullable=True),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"],
            ["index_symbols.project_id", "index_symbols.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_index_relationships_target",
        "index_relationships",
        ["project_id", "target_id"],
    )

    # Task/Council history intentionally survives Project deregistration. Target
    # snapshots and timings may be unknown; unknown never means zero.
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancellation_requested_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("reason", sa.String(40), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("execution_result", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "telemetry_status", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column("queue_duration_seconds", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_state",
        ),
        sa.CheckConstraint(
            "telemetry_status IN ('pending', 'recorded', 'unavailable')",
            name="ck_tasks_telemetry_status",
        ),
    )
    op.create_index("ix_tasks_created", "tasks", ["created_at", "task_id"])
    op.create_index(
        "ix_tasks_state_created", "tasks", ["state", "created_at", "task_id"]
    )
    for field in ("project_id", "agent_id", "worker_id"):
        op.create_index("ix_tasks_" + field.removesuffix("_id"), "tasks", [field])

    # Immutable terminal observations are retained independently of registration
    # and mutable Task execution JSON. No cascading history/identity foreign keys.
    op.create_table(
        "task_telemetry",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("error_category", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queue_duration_seconds", sa.Float(), nullable=True),
        sa.Column("execution_duration_seconds", sa.Float(), nullable=True),
        sa.Column("total_duration_seconds", sa.Float(), nullable=True),
        sa.Column("model_call_count", sa.Integer(), nullable=True),
        sa.Column("model_request_duration_seconds", sa.Float(), nullable=True),
        sa.Column("backend_total_duration_seconds", sa.Float(), nullable=True),
        sa.Column("model_load_duration_seconds", sa.Float(), nullable=True),
        sa.Column("prompt_evaluation_duration_seconds", sa.Float(), nullable=True),
        sa.Column("generation_duration_seconds", sa.Float(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("observed_prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("observed_completion_tokens", sa.Integer(), nullable=True),
        sa.Column("prompt_observed_turns", sa.Integer(), nullable=False),
        sa.Column("completion_observed_turns", sa.Integer(), nullable=False),
        sa.Column("token_usage_complete", sa.Boolean(), nullable=False),
        sa.Column("ttft_seconds", sa.Float(), nullable=True),
        sa.Column("tokens_per_second", sa.Float(), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), nullable=True),
        sa.Column("total_tool_duration_seconds", sa.Float(), nullable=True),
        sa.Column("tool_output_bytes", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "state IN ('completed', 'failed', 'cancelled')",
            name="ck_task_telemetry_state",
        ),
    )
    op.create_index(
        "ix_task_telemetry_created", "task_telemetry", ["created_at", "task_id"]
    )
    for field in ("project_id", "agent_id", "worker_id", "provider", "model", "state"):
        op.create_index(
            "ix_task_telemetry_" + field.removesuffix("_id") + "_created",
            "task_telemetry",
            [field, "created_at", "task_id"],
        )

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
        # NO ACTION protects complete historical membership; neither parent
        # deletion nor membership deletion can cascade into execution history.
        sa.ForeignKeyConstraint(["council_id"], ["councils.council_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
        sa.UniqueConstraint("council_id", "worker_id", name="uq_council_worker"),
        sa.UniqueConstraint("task_id", name="uq_council_task"),
        sa.CheckConstraint("ordinal >= 0 AND ordinal < 16", name="ck_council_ordinal"),
    )
    # Independent private ownership records survive deregistration and failed
    # executions so cleanup never depends on an authorized live Project path.
    op.create_table(
        "coding_workspaces",
        sa.Column("task_id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
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
        sa.UniqueConstraint("workspace_id", name="uq_coding_workspace_id"),
        sa.UniqueConstraint("branch_name", "repository_path", name="uq_coding_branch"),
        sa.CheckConstraint(
            "state IN ('provisioning', 'ready', 'completed', 'failed', 'cancelled', "
            "'interrupted', 'cleanup_pending', 'removed', 'suspicious')",
            name="ck_coding_workspace_state",
        ),
    )


def downgrade() -> None:
    # Drop dependent cache/membership tables before their parents. This is a
    # destructive return to an empty database, not a history-preserving rollback.
    for table in (
        "coding_workspaces",
        "council_participants",
        "councils",
        "task_telemetry",
        "tasks",
        "index_relationships",
        "index_symbols",
        "indexed_files",
        "project_indexes",
        "projects",
    ):
        op.drop_table(table)
