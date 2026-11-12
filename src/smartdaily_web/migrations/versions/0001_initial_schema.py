"""Create the original relational application schema."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("message_id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("discovered_at", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_deleted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.UniqueConstraint("type", "external_id", name="messages_type_external_id_key"),
        sa.CheckConstraint("is_deleted IN (0, 1)"),
    )
    op.create_index("messages_type_order_idx", "messages", ["type", "sort_order"])
    op.create_index("messages_discovered_at_idx", "messages", ["discovered_at"])

    op.create_table(
        "announcements",
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("community_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("create_date", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("start_date", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("end_date", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("is_top", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("type_id", sa.Integer()),
        sa.Column("type_name", sa.Text(), server_default=sa.text("'公告'"), nullable=False),
        sa.Column("already_read", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("extra_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint("is_top IN (0, 1)"),
        sa.CheckConstraint("already_read IN (0, 1)"),
    )
    op.create_table(
        "reminders",
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pushing_time", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("has_read", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("body", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("feature_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("target_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("extra_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint("has_read IN (0, 1)"),
    )
    op.create_index("reminders_pushing_time_idx", "reminders", ["pushing_time"])
    op.create_index("reminders_target_id_idx", "reminders", ["target_id"])

    op.create_table(
        "personal_notifications",
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("community", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("push_date", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("extra_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.create_table(
        "announcement_archives",
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("attachments_cached", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("attachments_cached IN (0, 1)"),
    )
    op.create_table(
        "message_attachments",
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("purpose", sa.Text(), primary_key=True),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("metadata_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('source', 'archive')", name="ck_message_attachments_purpose"
        ),
    )
    op.create_table(
        "devices",
        sa.Column("device_id", sa.Text(), primary_key=True),
        sa.Column("endpoint", sa.Text(), nullable=False, unique=True),
        sa.Column("p256dh", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("auth", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("user_agent", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("subscribed_at", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("create_time", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.create_table(
        "push_deliveries",
        sa.Column(
            "device_id",
            sa.Text(),
            sa.ForeignKey("devices.device_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "message_id",
            sa.Integer(),
            sa.ForeignKey("messages.message_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_attempt_at", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("sent_at", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("last_error", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint("status IN ('sent', 'failed')"),
    )
    op.create_index("push_deliveries_status_idx", "push_deliveries", ["device_id", "status"])
    op.create_table(
        "sync_state",
        sa.Column("singleton_id", sa.Integer(), primary_key=True),
        sa.Column("community_name", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("com_id", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("last_synced_at", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("last_full_sync_at", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("extra_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint("singleton_id = 1"),
    )
    op.execute("INSERT INTO sync_state(singleton_id) VALUES (1)")
    op.create_table("initialized_types", sa.Column("type", sa.Text(), primary_key=True))


def downgrade() -> None:
    op.drop_table("initialized_types")
    op.drop_table("sync_state")
    op.drop_index("push_deliveries_status_idx", table_name="push_deliveries")
    op.drop_table("push_deliveries")
    op.drop_table("devices")
    op.drop_table("message_attachments")
    op.drop_table("announcement_archives")
    op.drop_table("personal_notifications")
    op.drop_index("reminders_target_id_idx", table_name="reminders")
    op.drop_index("reminders_pushing_time_idx", table_name="reminders")
    op.drop_table("reminders")
    op.drop_table("announcements")
    op.drop_index("messages_discovered_at_idx", table_name="messages")
    op.drop_index("messages_type_order_idx", table_name="messages")
    op.drop_table("messages")
