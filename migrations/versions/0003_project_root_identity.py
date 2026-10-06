"""Record directory identity for new registrations; never trust legacy roots.

Revision ID: 0003_project_root_identity
Revises: 0002_project_index
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_project_root_identity"
down_revision = "0002_project_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("root_device", sa.Text(), nullable=True))
    op.add_column("projects", sa.Column("root_inode", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "root_inode")
    op.drop_column("projects", "root_device")
