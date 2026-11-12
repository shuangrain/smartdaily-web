"""SQLAlchemy persistence and Alembic-backed startup migrations."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, create_engine, delete, event, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    Announcement,
    AnnouncementArchive,
    DataMigration,
    Device,
    InitializedType,
    Message,
    MessageAttachment,
    PersonalNotification,
    PushDelivery,
    Reminder,
    SyncState,
)

logger = logging.getLogger(__name__)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStore:
    """Persist application records through ORM sessions and run versioned migrations."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.database_file = self.data_dir / "smartdaily.sqlite3"
        self.legacy_messages_file = self.data_dir / "messages.json"
        self.legacy_subscriptions_file = self.data_dir / "subscriptions.json"
        self.legacy_devices_dir = self.data_dir / "devices"
        self.attachments_dir = self.data_dir / "attachments"
        self.engine: Engine = create_engine(
            URL.create("sqlite", database=str(self.database_file)),
            connect_args={"timeout": 30},
        )
        event.listen(self.engine, "connect", self._configure_sqlite_connection)
        self.Session = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    @staticmethod
    def _configure_sqlite_connection(connection: Any, _record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    def _alembic_config(self) -> Config:
        config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
        if config_path.is_file():
            return Config(str(config_path))
        config = Config()
        config.set_main_option(
            "script_location", str(Path(__file__).resolve().parent / "migrations")
        )
        return config

    def _run_alembic(self, operation: str, revision: str) -> None:
        config = self._alembic_config()
        with self.engine.connect() as connection:
            config.attributes["connection"] = connection
            if operation == "stamp":
                command.stamp(config, revision)
            else:
                command.upgrade(config, revision)

    def _current_revision(self) -> str | None:
        if not inspect(self.engine).has_table("alembic_version"):
            return None
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one_or_none()

    def _legacy_schema_version(self) -> int | None:
        if not inspect(self.engine).has_table("schema_migrations"):
            return None
        with self.engine.connect() as connection:
            return connection.execute(
                text("SELECT MAX(version) FROM schema_migrations")
            ).scalar_one_or_none()

    def migrate(self) -> None:
        """Upgrade the schema, preserve subscriptions, and apply storage migrations once."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        target_revision = ScriptDirectory.from_config(self._alembic_config()).get_current_head()
        if not target_revision:
            raise RuntimeError("Alembic has no migration head")
        current_revision = self._current_revision()
        adopted_legacy_schema = False

        try:
            if current_revision is None:
                legacy_version = self._legacy_schema_version()
                has_message_schema = inspect(self.engine).has_table("messages")
                if legacy_version is not None and legacy_version >= 1 and has_message_schema:
                    logger.info(
                        "Adopting existing SQLite schema at Alembic baseline 0001 "
                        "(legacy version %d)",
                        legacy_version,
                    )
                    self._run_alembic("stamp", "0001")
                    current_revision = "0001"
                    adopted_legacy_schema = True

            if current_revision == target_revision:
                logger.info("Alembic schema is current at %s", target_revision)
            else:
                logger.info(
                    "Applying Alembic schema migrations (revision %s -> %s)",
                    current_revision or "base",
                    target_revision,
                )
                self._run_alembic("upgrade", "head")
                logger.info("Alembic schema migration complete at %s", target_revision)
        except Exception:
            logger.exception("Alembic schema migration failed")
            raise

        if adopted_legacy_schema:
            self._reset_full_sync_after_schema_adoption()
        self._log_skipped_legacy_message_import()
        self._migrate_legacy_device_subscriptions()
        self._migrate_legacy_attachment_storage()

    def _reset_full_sync_after_schema_adoption(self) -> None:
        name = "full_sync_after_alembic_adoption"
        with self.Session.begin() as session:
            if session.get(DataMigration, name):
                return
            state = session.get(SyncState, 1)
            if state is not None:
                state.last_full_sync_at = ""
            session.add(DataMigration(name=name, completed_at=_now_iso()))
        logger.info("Cleared last full sync time so the upgraded database gets a full sync")

    def _log_skipped_legacy_message_import(self) -> None:
        if not self.legacy_messages_file.is_file():
            return
        name = "legacy_messages_json_import_skipped"
        with self.Session.begin() as session:
            if session.get(DataMigration, name):
                return
            session.add(DataMigration(name=name, completed_at=_now_iso()))
        logger.info(
            "Skipped importing legacy messages.json; "
            "the first full sync will rebuild stored messages"
        )

    def _legacy_subscription_files(self) -> list[Path]:
        files = (
            sorted(self.legacy_devices_dir.glob("*.json"))
            if self.legacy_devices_dir.is_dir()
            else []
        )
        if self.legacy_subscriptions_file.is_file():
            files.append(self.legacy_subscriptions_file)
        return files

    @staticmethod
    def _load_legacy_subscription(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot migrate device subscription from {path}: {exc}") from exc

    def _migrate_legacy_device_subscriptions(self) -> None:
        name = "legacy_device_subscriptions"
        files = self._legacy_subscription_files()
        imported = 0
        skipped_existing = False
        with self.Session.begin() as session:
            if session.get(DataMigration, name):
                return

            existing_count = session.scalar(select(func.count()).select_from(Device)) or 0
            if existing_count:
                skipped_existing = True
            else:
                subscriptions: dict[str, dict[str, Any]] = {}
                for path in files:
                    payload = self._load_legacy_subscription(path)
                    entries = payload if isinstance(payload, list) else [payload]
                    for sub in entries:
                        if not isinstance(sub, dict) or not sub.get("endpoint"):
                            continue
                        endpoint = str(sub["endpoint"])
                        device_id = str(sub.get("device_id") or _device_id(endpoint))
                        if endpoint in subscriptions:
                            continue
                        subscriptions[endpoint] = {**sub, "device_id": device_id}

                for sub in subscriptions.values():
                    self._save_device(session, sub)
                    imported += 1

            session.add(DataMigration(name=name, completed_at=_now_iso()))

        removed = 0
        for path in files:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning(
                    "Could not remove migrated device subscription file %s: %s", path, exc
                )
        if self.legacy_devices_dir.is_dir():
            try:
                self.legacy_devices_dir.rmdir()
            except OSError:
                pass

        if imported:
            logger.info("Imported %d legacy device subscription(s) into SQLite", imported)
        elif skipped_existing:
            logger.info(
                "Kept %d existing SQLite device subscription(s); skipped stale legacy device data",
                existing_count,
            )
        if removed:
            logger.info("Removed %d legacy device subscription file(s)", removed)

    def _migrate_legacy_attachment_storage(self) -> None:
        name = "attachments_flattened_to_uuid_files"
        staged_files: list[tuple[Path, Path]] = []
        moved = 0
        missing = 0
        try:
            with self.Session.begin() as session:
                if session.get(DataMigration, name):
                    return
                rows = session.execute(
                    select(MessageAttachment, Message.external_id)
                    .join(Message, Message.message_id == MessageAttachment.message_id)
                    .where(
                        MessageAttachment.purpose == "archive",
                        MessageAttachment.stored_filename.is_(None),
                    )
                ).all()
                if not rows:
                    return

                for attachment, announcement_id in rows:
                    metadata = _json_load(attachment.metadata_json, {})
                    old_filename = str(metadata.get("local_file") or "").strip()
                    if not old_filename or Path(old_filename).name != old_filename:
                        url_parts = str(metadata.get("url") or "").rstrip("/").split("/")
                        old_filename = url_parts[-1] if len(url_parts) >= 2 else ""
                    if not old_filename or Path(old_filename).name != old_filename:
                        missing += 1
                        continue

                    source = (self.attachments_dir / announcement_id / old_filename).resolve()
                    expected_parent = (self.attachments_dir / announcement_id).resolve()
                    if source.parent != expected_parent or not source.is_file():
                        missing += 1
                        continue

                    stored_filename = str(uuid.uuid4())
                    destination = self.attachments_dir / stored_filename
                    shutil.copy2(source, destination)
                    staged_files.append((source, destination))

                    original_name = str(
                        metadata.get("original_name")
                        or metadata.get("filename")
                        or metadata.get("name")
                        or old_filename
                    )
                    mime_type = str(
                        metadata.get("mime_type") or mimetypes.guess_type(original_name)[0] or ""
                    )
                    metadata.update(
                        {
                            "filename": original_name,
                            "original_name": original_name,
                            "stored_filename": stored_filename,
                            "local_file": stored_filename,
                            "mime_type": mime_type,
                            "url": f"/api/attachments/{stored_filename}",
                        }
                    )
                    attachment.metadata_json = _json_dump(metadata)
                    attachment.original_name = original_name
                    attachment.stored_filename = stored_filename
                    attachment.mime_type = mime_type
                    moved += 1

                session.add(DataMigration(name=name, completed_at=_now_iso()))
        except Exception:
            for _source, destination in staged_files:
                destination.unlink(missing_ok=True)
            raise

        for source, _destination in staged_files:
            try:
                source.unlink(missing_ok=True)
                try:
                    source.parent.rmdir()
                except OSError:
                    pass
            except OSError as exc:
                logger.warning("Could not remove old attachment file %s: %s", source, exc)
        if moved or missing:
            logger.info(
                "Flattened legacy attachment storage: moved=%d, missing_files=%d",
                moved,
                missing,
            )

    def remove_legacy_messages_file_after_full_sync(self) -> bool:
        """Remove the old message export only after a full sync has been saved."""
        if not self.legacy_messages_file.is_file():
            return False
        try:
            self.legacy_messages_file.unlink()
        except OSError as exc:
            logger.warning("Could not remove legacy messages.json after full sync: %s", exc)
            return False
        logger.info("Removed legacy messages.json after successful full sync")
        return True

    def _save_message(self, session: Session, message: dict[str, Any], sort_order: int) -> None:
        msg_type = str(message.get("type") or "")
        item = message.get("item") if isinstance(message.get("item"), dict) else {}
        external_id = str(item.get("id") or message.get("id") or "")
        if msg_type not in {"announcement", "reminder", "personal"} or not external_id:
            return

        row = session.scalar(
            select(Message).where(Message.type == msg_type, Message.external_id == external_id)
        )
        if row is None:
            row = Message(type=msg_type, external_id=external_id, discovered_at="")
            session.add(row)
        row.discovered_at = str(message.get("discovered_at") or "")
        row.sort_order = sort_order
        row.is_deleted = int(bool(message.get("is_deleted", item.get("is_deleted", False))))
        session.flush()

        item_fields = dict(item)
        item_fields.pop("id", None)
        item_fields.pop("is_deleted", None)
        source_files = item_fields.pop("files", [])

        if msg_type == "announcement":
            if "com_id" in item_fields:
                item_fields["community_id"] = item_fields.pop("com_id")
            typed_record = session.get(Announcement, row.message_id)
            if typed_record is None:
                typed_record = Announcement(message_id=row.message_id)
                session.add(typed_record)
            self._save_typed_record(
                typed_record,
                item_fields,
                (
                    "community_id",
                    "title",
                    "content",
                    "create_date",
                    "start_date",
                    "end_date",
                    "is_top",
                    "type_id",
                    "type_name",
                    "already_read",
                ),
            )
            archive = session.get(AnnouncementArchive, row.message_id)
            if archive is None:
                archive = AnnouncementArchive(message_id=row.message_id)
                session.add(archive)
            archive.attachments_cached = int(bool(message.get("attachments_cached", False)))
        elif msg_type == "reminder":
            typed_record = session.get(Reminder, row.message_id)
            if typed_record is None:
                typed_record = Reminder(message_id=row.message_id)
                session.add(typed_record)
            self._save_typed_record(
                typed_record,
                item_fields,
                ("pushing_time", "has_read", "title", "body", "feature_name", "target_id"),
            )
        else:
            typed_record = session.get(PersonalNotification, row.message_id)
            if typed_record is None:
                typed_record = PersonalNotification(message_id=row.message_id)
                session.add(typed_record)
            self._save_typed_record(
                typed_record,
                item_fields,
                ("community", "content", "push_date"),
            )

        session.execute(
            delete(MessageAttachment).where(MessageAttachment.message_id == row.message_id)
        )
        for purpose, attachments in (
            ("source", source_files),
            ("archive", message.get("attachments", [])),
        ):
            for index, attachment in enumerate(attachments or [], start=1):
                if not isinstance(attachment, dict):
                    continue
                position = int(attachment.get("index", index))
                original_name = str(
                    attachment.get("original_name")
                    or attachment.get("filename")
                    or attachment.get("name")
                    or ""
                )
                session.add(
                    MessageAttachment(
                        message_id=row.message_id,
                        purpose=purpose,
                        position=position,
                        metadata_json=_json_dump(attachment),
                        original_name=original_name,
                        stored_filename=attachment.get("stored_filename"),
                        mime_type=str(attachment.get("mime_type") or ""),
                    )
                )

    @staticmethod
    def _save_typed_record(
        record: Announcement | Reminder | PersonalNotification,
        item: dict[str, Any],
        columns: tuple[str, ...],
    ) -> None:
        defaults: dict[str, Any] = {
            "community_id": "",
            "title": "",
            "content": "",
            "create_date": "",
            "start_date": "",
            "end_date": "",
            "is_top": False,
            "type_name": "公告",
            "already_read": True,
            "pushing_time": "",
            "has_read": True,
            "body": "",
            "feature_name": "",
            "target_id": "",
            "community": "",
            "push_date": "",
        }
        known = set(columns)
        extras = {key: value for key, value in item.items() if key not in known}
        for column in columns:
            value = item.get(column)
            if value is None:
                value = defaults.get(column)
            if column in {"is_top", "already_read", "has_read"}:
                value = int(bool(value))
            if column == "type_name":
                value = str(value or "公告")
            if column == "type_id" and value is None:
                continue
            setattr(record, column, value)
        record.extra_json = _json_dump(extras)

    @staticmethod
    def _save_metadata(session: Session, metadata: dict[str, Any]) -> None:
        if not isinstance(metadata, dict):
            return
        state = session.get(SyncState, 1)
        if state is None:
            state = SyncState(singleton_id=1)
            session.add(state)
        known = {"community_name", "com_id", "last_synced_at", "last_full_sync_at"}
        for key in known:
            value = metadata.get(key)
            if value is not None:
                setattr(state, key, str(value))
        state.extra_json = _json_dump(
            {key: value for key, value in metadata.items() if key not in known}
        )

    @staticmethod
    def _save_device(session: Session, sub: dict[str, Any]) -> str:
        endpoint = str(sub.get("endpoint") or "")
        if not endpoint:
            return ""
        device_id = str(sub.get("device_id") or _device_id(endpoint))
        row = session.get(Device, device_id)
        if row is None:
            row = session.scalar(select(Device).where(Device.endpoint == endpoint))
        if row is None:
            row = Device(device_id=device_id, endpoint=endpoint)
            session.add(row)
        keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
        row.endpoint = endpoint
        row.p256dh = str(keys.get("p256dh") or "")
        row.auth = str(keys.get("auth") or "")
        row.user_agent = str(sub.get("user_agent") or "")
        row.subscribed_at = str(sub.get("subscribed_at") or "")
        row.create_time = str(sub.get("create_time") or "")
        session.flush()
        return row.device_id

    @staticmethod
    def _record_legacy_delivery(session: Session, device_id: str, uid: str) -> None:
        msg_type, separator, external_id = str(uid).partition(":")
        if not separator:
            return
        message = session.scalar(
            select(Message).where(Message.type == msg_type, Message.external_id == external_id)
        )
        if message is None:
            return
        key = (device_id, message.message_id)
        if session.get(PushDelivery, key) is None:
            session.add(
                PushDelivery(device_id=device_id, message_id=message.message_id, status="sent")
            )

    def load_message_store(self) -> dict[str, Any]:
        with self.Session() as session:
            messages = session.scalars(
                select(Message).order_by(Message.sort_order, Message.message_id)
            ).all()
            ids = [message.message_id for message in messages]
            initialized = session.scalars(select(InitializedType).order_by(text("rowid"))).all()
            state = session.get(SyncState, 1)
            typed: dict[type[Any], dict[int, Any]] = {}
            attachment_rows: dict[int, list[MessageAttachment]] = {}
            if ids:
                for model in (Announcement, Reminder, PersonalNotification, AnnouncementArchive):
                    typed[model] = {
                        row.message_id: row
                        for row in session.scalars(
                            select(model).where(model.message_id.in_(ids))
                        ).all()
                    }
                for row in session.scalars(
                    select(MessageAttachment)
                    .where(MessageAttachment.message_id.in_(ids))
                    .order_by(
                        MessageAttachment.message_id,
                        MessageAttachment.purpose,
                        MessageAttachment.position,
                    )
                ).all():
                    attachment_rows.setdefault(row.message_id, []).append(row)

            output = [
                self._load_message(row, typed, attachment_rows.get(row.message_id, []))
                for row in messages
            ]
            metadata = _json_load(state.extra_json, {}) if state else {}
            if state:
                for key in ("community_name", "com_id", "last_synced_at", "last_full_sync_at"):
                    value = getattr(state, key)
                    if value:
                        metadata[key] = value
        return {
            "initialized_types": [row.type for row in initialized],
            "messages": output,
            "metadata": metadata,
        }

    @staticmethod
    def _load_message(
        row: Message,
        typed: dict[type[Any], dict[int, Any]],
        attachment_rows: list[MessageAttachment],
    ) -> dict[str, Any]:
        msg_type = row.type
        model_by_type: dict[str, type[Any]] = {
            "announcement": Announcement,
            "reminder": Reminder,
            "personal": PersonalNotification,
        }
        model = model_by_type.get(msg_type)
        typed_row = typed.get(model, {}).get(row.message_id) if model else None
        item: dict[str, Any] = _json_load(typed_row.extra_json, {}) if typed_row else {}
        fields_by_type: dict[str, tuple[tuple[str, str], ...]] = {
            "announcement": (
                ("com_id", "community_id"),
                ("title", "title"),
                ("content", "content"),
                ("create_date", "create_date"),
                ("start_date", "start_date"),
                ("end_date", "end_date"),
                ("is_top", "is_top"),
                ("type_id", "type_id"),
                ("type_name", "type_name"),
                ("already_read", "already_read"),
            ),
            "reminder": tuple(
                (key, key)
                for key in (
                    "pushing_time",
                    "has_read",
                    "title",
                    "body",
                    "feature_name",
                    "target_id",
                )
            ),
            "personal": tuple((key, key) for key in ("community", "content", "push_date")),
        }
        if typed_row:
            for output_key, column in fields_by_type[msg_type]:
                value = getattr(typed_row, column)
                item[output_key] = (
                    bool(value) if column in {"is_top", "already_read", "has_read"} else value
                )
        item["id"] = row.external_id
        item["is_deleted"] = bool(row.is_deleted)

        source_attachments = [record for record in attachment_rows if record.purpose == "source"]
        if source_attachments or msg_type in ("reminder", "personal"):
            item["files"] = [
                SQLiteStore._attachment_payload(record) for record in source_attachments
            ]

        message = {
            "uid": f"{msg_type}:{row.external_id}",
            "type": msg_type,
            "id": row.external_id,
            "discovered_at": row.discovered_at,
            "is_deleted": bool(row.is_deleted),
            "item": item,
        }
        archive_attachments = [record for record in attachment_rows if record.purpose == "archive"]
        if archive_attachments:
            message["attachments"] = [
                SQLiteStore._attachment_payload(record) for record in archive_attachments
            ]
        if msg_type == "announcement":
            archive = typed.get(AnnouncementArchive, {}).get(row.message_id)
            message["attachments_cached"] = bool(archive.attachments_cached) if archive else False
        return message

    @staticmethod
    def _attachment_payload(record: MessageAttachment) -> dict[str, Any]:
        metadata = _json_load(record.metadata_json, {})
        if record.stored_filename:
            metadata.update(
                {
                    "filename": record.original_name or metadata.get("filename", ""),
                    "original_name": record.original_name,
                    "stored_filename": record.stored_filename,
                    "mime_type": record.mime_type,
                    "url": f"/api/attachments/{record.stored_filename}",
                }
            )
        return metadata

    def save_message_store(self, store: dict[str, Any]) -> None:
        with self.Session.begin() as session:
            for msg_type in dict.fromkeys(store.get("initialized_types", [])):
                if session.get(InitializedType, str(msg_type)) is None:
                    session.add(InitializedType(type=str(msg_type)))
            for sort_order, message in enumerate(store.get("messages", [])):
                self._save_message(session, message, sort_order)
            self._save_metadata(session, store.get("metadata", {}))

    def _load_device(self, session: Session, row: Device) -> dict[str, Any]:
        pushed = session.execute(
            select(Message.type, Message.external_id)
            .join(PushDelivery, PushDelivery.message_id == Message.message_id)
            .where(PushDelivery.device_id == row.device_id, PushDelivery.status == "sent")
            .order_by(Message.message_id)
        ).all()
        return {
            "device_id": row.device_id,
            "endpoint": row.endpoint,
            "keys": {"p256dh": row.p256dh, "auth": row.auth},
            "user_agent": row.user_agent,
            "subscribed_at": row.subscribed_at,
            "create_time": row.create_time,
            "pushed_message_uids": [
                f"{message_type}:{external_id}" for message_type, external_id in pushed
            ],
        }

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self.Session() as session:
            row = session.get(Device, device_id)
            return self._load_device(session, row) if row else None

    def list_devices(self) -> list[dict[str, Any]]:
        with self.Session() as session:
            rows = session.scalars(select(Device).order_by(Device.device_id)).all()
            return [self._load_device(session, row) for row in rows]

    def save_device(self, sub: dict[str, Any]) -> None:
        with self.Session.begin() as session:
            device_id = self._save_device(session, sub)
            if "pushed_message_uids" in sub:
                for uid in dict.fromkeys(
                    str(value) for value in sub.get("pushed_message_uids", [])
                ):
                    self._record_legacy_delivery(session, device_id, uid)

    def delete_device(self, device_id: str) -> None:
        with self.Session.begin() as session:
            row = session.get(Device, device_id)
            if row:
                session.delete(row)

    def record_delivery(self, device_id: str, uid: str, status: str) -> None:
        msg_type, separator, external_id = uid.partition(":")
        if not separator:
            return
        now = _now_iso()
        with self.Session.begin() as session:
            message = session.scalar(
                select(Message).where(Message.type == msg_type, Message.external_id == external_id)
            )
            if message is None:
                return
            delivery = session.get(PushDelivery, (device_id, message.message_id))
            if delivery is None:
                delivery = PushDelivery(
                    device_id=device_id,
                    message_id=message.message_id,
                    status=status,
                    attempts=1,
                    last_attempt_at=now,
                    sent_at=now if status == "sent" else "",
                )
                session.add(delivery)
            else:
                delivery.status = status
                delivery.attempts += 1
                delivery.last_attempt_at = now
                if status == "sent":
                    delivery.sent_at = now

    def get_attachment_metadata(self, stored_filename: str) -> dict[str, str] | None:
        with self.Session() as session:
            row = session.scalar(
                select(MessageAttachment).where(
                    MessageAttachment.stored_filename == stored_filename,
                    MessageAttachment.purpose == "archive",
                )
            )
            if row is None:
                return None
            return {
                "original_name": row.original_name,
                "stored_filename": row.stored_filename or "",
                "mime_type": row.mime_type,
            }
