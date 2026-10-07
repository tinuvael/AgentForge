"""Cache deterministic Python structure for registered projects.

Revision ID: 0002_project_index
Revises: 0001_projects
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_project_index"
down_revision = "0001_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_indexes",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_head", sa.String(40)),
        sa.PrimaryKeyConstraint("project_id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "indexed_files",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("language", sa.String(20), nullable=False),
        sa.Column("module_name", sa.Text(), nullable=False),
        sa.Column("observed_hash", sa.String(64), nullable=False),
        sa.Column("parsed_hash", sa.String(64)),
        sa.Column("parse_error", sa.Text()),
        sa.PrimaryKeyConstraint("project_id", "relative_path"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "index_symbols",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer()),
        sa.Column("parent_id", sa.Text()),
        sa.Column("import_candidate", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", "id"),
        sa.ForeignKeyConstraint(
            ["project_id", "relative_path"],
            ["indexed_files.project_id", "indexed_files.relative_path"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_index_symbols_name", "index_symbols", ["project_id", "name"])
    op.create_table(
        "index_relationships",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("target_text", sa.Text(), nullable=False),
        sa.Column("target_key", sa.Text()),
        sa.Column("target_id", sa.Text()),
        sa.PrimaryKeyConstraint("project_id", "relative_path", "ordinal"),
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


def downgrade() -> None:
    op.drop_table("index_relationships")
    op.drop_table("index_symbols")
    op.drop_table("indexed_files")
    op.drop_table("project_indexes")
