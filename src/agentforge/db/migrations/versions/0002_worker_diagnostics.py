"""Bounded operator diagnostic checkpoints, independent of Task telemetry.

Revision ID: 0002_worker_diagnostics
Revises: 0001_initial
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_worker_diagnostics"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_diagnostic_observations",
        sa.Column("worker_id", sa.Text(), primary_key=True),
        sa.Column("probe_kind", sa.String(20), primary_key=True),
        sa.Column("configuration_fingerprint", sa.String(64), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latest", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "probe_kind IN ('health', 'generation', 'tools', 'streaming')",
            name="ck_worker_diagnostics_probe_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table("worker_diagnostic_observations")
