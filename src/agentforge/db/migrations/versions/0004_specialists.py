"""Durable specialist jobs and private artifact metadata (no binary payloads)."""

import sqlalchemy as sa
from alembic import op

revision = "0004_specialists"
down_revision = "0003_capability_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "specialist_store",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("root_identity", sa.JSON(), nullable=False),
    )
    op.create_table(
        "tool_jobs",
        sa.Column("tool_job_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.String(100), nullable=False),
        sa.Column("owner_scope", sa.String(110), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_scope", "idempotency_key", name="uq_tool_job_key"),
        sa.UniqueConstraint("owner_scope", "request_id", name="uq_tool_job_request"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'completed', 'failed', 'cancelled', "
            "'interrupted')",
            name="ck_tool_jobs_state",
        ),
    )
    op.create_index(
        "ix_tool_jobs_queue", "tool_jobs", ["state", "created_at", "tool_job_id"]
    )
    op.create_index("ix_tool_jobs_project", "tool_jobs", ["project_id", "created_at"])
    op.create_table(
        "artifacts",
        sa.Column("artifact_id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tool_job_id",
            sa.Uuid(),
            sa.ForeignKey("tool_jobs.tool_job_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "state IN ('staged', 'ready', 'expired')", name="ck_artifacts_state"
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_artifacts_size"),
    )
    op.create_index("ix_artifacts_project", "artifacts", ["project_id", "artifact_id"])


def downgrade():
    op.drop_table("artifacts")
    op.drop_table("tool_jobs")
    op.drop_table("specialist_store")
