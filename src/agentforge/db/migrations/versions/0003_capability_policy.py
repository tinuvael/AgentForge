"""Project egress, immutable execution binding and connection resource evidence.

Revision ID: 0003_capability_policy
Revises: 0002_gateway_client
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_capability_policy"
down_revision = "0002_gateway_client"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "projects",
        sa.Column(
            "egress_policy",
            sa.JSON(),
            nullable=False,
            server_default='{"mode":"local_only","allowed_connections":[]}',
        ),
    )
    op.add_column("tasks", sa.Column("provider_connection", sa.Text(), nullable=True))
    op.add_column(
        "tasks", sa.Column("target_fingerprint", sa.String(64), nullable=True)
    )
    op.add_column(
        "tasks",
        sa.Column("task_policy", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "connection_resource_observations",
        sa.Column("connection_id", sa.Text(), primary_key=True),
        sa.Column("requests_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "usage_unknown", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("in_flight", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(40), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "requests_used >= 0 AND tokens_used >= 0 AND in_flight >= 0",
            name="ck_connection_resources_nonnegative",
        ),
    )


def downgrade():
    op.drop_table("connection_resource_observations")
    op.drop_column("tasks", "task_policy")
    op.drop_column("tasks", "target_fingerprint")
    op.drop_column("tasks", "provider_connection")
    op.drop_column("projects", "egress_policy")
