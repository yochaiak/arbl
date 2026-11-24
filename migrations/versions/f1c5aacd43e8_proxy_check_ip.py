"""Store the proxy check IP separately on session attempts.

Revision ID: f1c5aacd43e8
Revises: 92fc8f4de2a7
"""

import sqlalchemy as sa
from alembic import op

revision = "f1c5aacd43e8"
down_revision = "92fc8f4de2a7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("session_attempts", sa.Column("proxy_check_ip", sa.String(length=45), nullable=True))


def downgrade():
    op.drop_column("session_attempts", "proxy_check_ip")
