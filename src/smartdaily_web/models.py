"""SQLAlchemy ORM models for persistent application data."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("type", "external_id", name="messages_type_external_id_key"),
        CheckConstraint("is_deleted IN (0, 1)"),
        Index("messages_type_order_idx", "type", "sort_order"),
        Index("messages_discovered_at_idx", "discovered_at"),
    )

    message_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    discovered_at: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_deleted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (
        CheckConstraint("is_top IN (0, 1)"),
        CheckConstraint("already_read IN (0, 1)"),
    )

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="CASCADE"), primary_key=True
    )
    community_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    create_date: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    start_date: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    end_date: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    is_top: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    type_id: Mapped[int | None] = mapped_column(Integer)
    type_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'公告'"))
    already_read: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint("has_read IN (0, 1)"),
        Index("reminders_pushing_time_idx", "pushing_time"),
        Index("reminders_target_id_idx", "target_id"),
    )

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="CASCADE"), primary_key=True
    )
    pushing_time: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    has_read: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    feature_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    target_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))


class PersonalNotification(Base):
    __tablename__ = "personal_notifications"

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="CASCADE"), primary_key=True
    )
    community: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    push_date: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))


class AnnouncementArchive(Base):
    __tablename__ = "announcement_archives"
    __table_args__ = (CheckConstraint("attachments_cached IN (0, 1)"),)

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="CASCADE"), primary_key=True
    )
    attachments_cached: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class MessageAttachment(Base):
    __tablename__ = "message_attachments"
    __table_args__ = (
        CheckConstraint("purpose IN ('source', 'archive')", name="ck_message_attachments_purpose"),
        Index("message_attachments_stored_filename_idx", "stored_filename", unique=True),
    )

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="CASCADE"), primary_key=True
    )
    purpose: Mapped[str] = mapped_column(Text, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))
    original_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    stored_filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(Text, primary_key=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    auth: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    user_agent: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    subscribed_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    create_time: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class PushDelivery(Base):
    __tablename__ = "push_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('sent', 'failed')"),
        Index("push_deliveries_status_idx", "device_id", "status"),
    )

    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), primary_key=True
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.message_id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_attempt_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    sent_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    last_error: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))


class SyncState(Base):
    __tablename__ = "sync_state"
    __table_args__ = (CheckConstraint("singleton_id = 1"),)

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, server_default=text("1"))
    community_name: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    com_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    last_synced_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    last_full_sync_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    extra_json: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'{}'"))


class InitializedType(Base):
    __tablename__ = "initialized_types"

    type: Mapped[str] = mapped_column(Text, primary_key=True)


class DataMigration(Base):
    __tablename__ = "data_migrations"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    completed_at: Mapped[str] = mapped_column(Text, nullable=False)
