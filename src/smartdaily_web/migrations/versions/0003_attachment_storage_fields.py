"""Store attachment names and UUID paths as structured columns."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table(
        "message_attachments",
        table_args=(
            sa.CheckConstraint(
                "purpose IN ('source', 'archive')", name="ck_message_attachments_purpose"
            ),
        ),
    ) as batch_op:
        batch_op.add_column(
            sa.Column("original_name", sa.Text(), server_default=sa.text("''"), nullable=False)
        )
        batch_op.add_column(sa.Column("stored_filename", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("mime_type", sa.Text(), server_default=sa.text("''"), nullable=False)
        )
    op.create_index(
        "message_attachments_stored_filename_idx",
        "message_attachments",
        ["stored_filename"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("message_attachments_stored_filename_idx", table_name="message_attachments")
    with op.batch_alter_table("message_attachments") as batch_op:
        batch_op.drop_column("mime_type")
        batch_op.drop_column("stored_filename")
        batch_op.drop_column("original_name")
