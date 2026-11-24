"""Store attempt recording state and encrypted video segment metadata.

Revision ID: 8ab08e4fe309
Revises: f1c5aacd43e8
"""

import sqlalchemy as sa
from alembic import op

revision = "8ab08e4fe309"
down_revision = "f1c5aacd43e8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("session_attempts", sa.Column("recording_status", sa.String(length=16), nullable=True))
    op.add_column("session_artifacts", sa.Column("attempt_id", sa.String(length=36), nullable=True))
    op.add_column("session_artifacts", sa.Column("video_sequence", sa.Integer(), nullable=True))
    op.add_column("session_artifacts", sa.Column("video_start_ms", sa.Integer(), nullable=True))
    op.add_column("session_artifacts", sa.Column("video_duration_ms", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_session_artifacts_attempt_id", "session_artifacts", "session_attempts", ["attempt_id"], ["id"]
    )
    op.create_unique_constraint(
        "uq_session_artifacts_attempt_sequence", "session_artifacts", ["attempt_id", "video_sequence"]
    )


def downgrade():
    # The pre-recording API treats artifacts without attempt IDs as viewer-accessible.
    # Expire videos before dropping the fields that enforce restricted access.
    op.execute(
        "UPDATE session_artifacts SET expires_at = '1970-01-01T00:00:00+00:00' "
        "WHERE attempt_id IS NOT NULL AND deleted_at IS NULL"
    )
    op.drop_constraint("uq_session_artifacts_attempt_sequence", "session_artifacts", type_="unique")
    op.drop_constraint("fk_session_artifacts_attempt_id", "session_artifacts", type_="foreignkey")
    op.drop_column("session_artifacts", "video_duration_ms")
    op.drop_column("session_artifacts", "video_start_ms")
    op.drop_column("session_artifacts", "video_sequence")
    op.drop_column("session_artifacts", "attempt_id")
    op.drop_column("session_attempts", "recording_status")
