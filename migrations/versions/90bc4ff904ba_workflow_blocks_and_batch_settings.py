"""Store workflow block revisions and per-session launch settings.

Revision ID: 90bc4ff904ba
Revises: 8ab08e4fe309
"""

import sqlalchemy as sa
from alembic import op

revision = "90bc4ff904ba"
down_revision = "8ab08e4fe309"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("browser_sessions", sa.Column("queue_reason", sa.String(40)))
    op.add_column("browser_sessions", sa.Column("next_attempt_at", sa.String(40)))
    op.add_column("session_batch_items", sa.Column("launch_config", sa.JSON()))
    op.create_table(
        "workflow_blocks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    op.create_index("ix_workflow_blocks_project_id", "workflow_blocks", ["project_id"])
    op.create_table(
        "workflow_block_versions",
        sa.Column("block_id", sa.String(36), sa.ForeignKey("workflow_blocks.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False),
    )


def downgrade():
    op.drop_table("workflow_block_versions")
    op.drop_index("ix_workflow_blocks_project_id", table_name="workflow_blocks")
    op.drop_table("workflow_blocks")
    op.drop_column("session_batch_items", "launch_config")
    op.drop_column("browser_sessions", "next_attempt_at")
    op.drop_column("browser_sessions", "queue_reason")
