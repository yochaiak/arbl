"""Store the client IP on trusted provider observations.

Revision ID: 92fc8f4de2a7
Revises: ee91f6008ed8
"""

import sqlalchemy as sa
from alembic import op

revision = "92fc8f4de2a7"
down_revision = "ee91f6008ed8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("provider_observations", sa.Column("observed_ip", sa.String(length=45), nullable=True))


def downgrade():
    op.drop_column("provider_observations", "observed_ip")
