"""Persist the submitting gateway client with each Task, including Council Tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0002_gateway_client"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing trusted-operator Tasks have no client owner and require an explicit
    # shared-history grant. Never infer ownership from requests or repository text.
    op.add_column("tasks", sa.Column("client_id", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "client_id")
