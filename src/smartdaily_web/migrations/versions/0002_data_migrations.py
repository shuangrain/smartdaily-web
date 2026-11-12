"""Track one-time application data migrations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_migrations",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("completed_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("data_migrations")
