"""Code-defined SQLite schema and migrations for persistent application data."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


class SQLiteStore:
    """Persist messages, devices, deliveries, and sync state in one SQLite file."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.database_file = data_dir / "smartdaily.sqlite3"
        self.legacy_messages_file = data_dir / "messages.json"
        self.legacy_subscriptions_file = data_dir / "subscriptions.json"
        self.legacy_devices_dir = data_dir / "devices"

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_file, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def migrate(self) -> None:
        """Apply every pending code-defined migration, including legacy JSON import."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )

        migrations = (
            (1, "create_relational_schema", self._create_relational_schema),
            (2, "import_legacy_json", self._import_legacy_json),
        )
        for version, name, migration in migrations:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                applied = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied:
                    continue
                migration(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.now(timezone.utc).isoformat()),
                )

    @staticmethod
    def _create_relational_schema(connection: sqlite3.Connection) -> None:
        schema = """
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
                UNIQUE(type, external_id)
            );

            CREATE INDEX messages_type_order_idx ON messages(type, sort_order);
            CREATE INDEX messages_discovered_at_idx ON messages(discovered_at);

            CREATE TABLE announcements (
                message_id INTEGER PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
                community_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                create_date TEXT NOT NULL DEFAULT '',
                start_date TEXT NOT NULL DEFAULT '',
                end_date TEXT NOT NULL DEFAULT '',
                is_top INTEGER NOT NULL DEFAULT 0 CHECK (is_top IN (0, 1)),
                type_id INTEGER,
                type_name TEXT NOT NULL DEFAULT '公告',
                already_read INTEGER NOT NULL DEFAULT 1 CHECK (already_read IN (0, 1)),
                extra_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE reminders (
                message_id INTEGER PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
                pushing_time TEXT NOT NULL DEFAULT '',
                has_read INTEGER NOT NULL DEFAULT 1 CHECK (has_read IN (0, 1)),
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                feature_name TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX reminders_pushing_time_idx ON reminders(pushing_time);
            CREATE INDEX reminders_target_id_idx ON reminders(target_id);

            CREATE TABLE personal_notifications (
                message_id INTEGER PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
                community TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                push_date TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE announcement_archives (
                message_id INTEGER PRIMARY KEY REFERENCES messages(message_id) ON DELETE CASCADE,
                attachments_cached INTEGER NOT NULL DEFAULT 0 CHECK (attachments_cached IN (0, 1))
            );

            CREATE TABLE message_attachments (
                message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
                purpose TEXT NOT NULL CHECK (purpose IN ('source', 'archive')),
                position INTEGER NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(message_id, purpose, position)
            );

            CREATE TABLE devices (
                device_id TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL DEFAULT '',
                auth TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                subscribed_at TEXT NOT NULL DEFAULT '',
                create_time TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE push_deliveries (
                device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(device_id, message_id)
            );

            CREATE INDEX push_deliveries_status_idx ON push_deliveries(device_id, status);

            CREATE TABLE sync_state (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                community_name TEXT NOT NULL DEFAULT '',
                com_id TEXT NOT NULL DEFAULT '',
                last_synced_at TEXT NOT NULL DEFAULT '',
                last_full_sync_at TEXT NOT NULL DEFAULT '',
                extra_json TEXT NOT NULL DEFAULT '{}'
            );

            INSERT INTO sync_state(singleton_id) VALUES (1);

            CREATE TABLE initialized_types (
                type TEXT PRIMARY KEY
            );
            """
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    def _import_legacy_json(self, connection: sqlite3.Connection) -> None:
        initialized_types: list[str] = []
        metadata: dict[str, Any] = {}
        if self.legacy_messages_file.is_file():
            try:
                legacy = json.loads(self.legacy_messages_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Cannot migrate {self.legacy_messages_file}: {exc}") from exc
            if isinstance(legacy, list):
                messages = legacy
                initialized_types = ["announcement", "personal", "reminder"]
            else:
                messages = legacy.get("messages", [])
                initialized_types = legacy.get("initialized_types", [])
                metadata = legacy.get("metadata", {})
            for sort_order, message in enumerate(messages):
                self._save_message(connection, message, sort_order)
            self._save_metadata(connection, metadata)

        for msg_type in initialized_types:
            connection.execute(
                "INSERT OR IGNORE INTO initialized_types(type) VALUES (?)", (str(msg_type),)
            )

        subscriptions: dict[str, dict[str, Any]] = {}
        if self.legacy_devices_dir.is_dir():
            for path in sorted(self.legacy_devices_dir.glob("*.json")):
                try:
                    sub = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"Cannot migrate {path}: {exc}") from exc
                if sub.get("endpoint"):
                    sub.setdefault("device_id", path.stem)
                    subscriptions[sub["device_id"]] = sub

        if self.legacy_subscriptions_file.is_file():
            try:
                legacy_subscriptions = json.loads(
                    self.legacy_subscriptions_file.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"Cannot migrate {self.legacy_subscriptions_file}: {exc}"
                ) from exc
            if isinstance(legacy_subscriptions, list):
                for sub in legacy_subscriptions:
                    endpoint = str(sub.get("endpoint") or "")
                    if endpoint:
                        device_id = str(sub.get("device_id") or _device_id(endpoint))
                        sub["device_id"] = device_id
                        subscriptions.setdefault(device_id, sub)

        message_rows = connection.execute(
            "SELECT type, external_id FROM messages ORDER BY sort_order, message_id"
        ).fetchall()
        existing_message_uids = [f"{row['type']}:{row['external_id']}" for row in message_rows]
        for device_id, sub in subscriptions.items():
            self._save_device(connection, sub, device_id=device_id)
            if "pushed_message_uids" in sub:
                pushed_uids = sub.get("pushed_message_uids") or []
            else:
                # Legacy devices without a delivery list treated current history as baseline.
                pushed_uids = existing_message_uids
            for uid in pushed_uids:
                self._record_legacy_delivery(connection, device_id, str(uid))

    @staticmethod
    def _save_metadata(connection: sqlite3.Connection, metadata: dict[str, Any]) -> None:
        if not isinstance(metadata, dict):
            return
        known = {"community_name", "com_id", "last_synced_at", "last_full_sync_at"}
        extra = {key: value for key, value in metadata.items() if key not in known}
        connection.execute(
            """
            UPDATE sync_state SET community_name=?, com_id=?, last_synced_at=?,
                last_full_sync_at=?, extra_json=? WHERE singleton_id=1
            """,
            (
                str(metadata.get("community_name") or ""),
                str(metadata.get("com_id") or ""),
                str(metadata.get("last_synced_at") or ""),
                str(metadata.get("last_full_sync_at") or ""),
                _json_dump(extra),
            ),
        )

    def _save_message(
        self, connection: sqlite3.Connection, message: dict[str, Any], sort_order: int
    ) -> None:
        msg_type = str(message.get("type") or "")
        item = message.get("item") if isinstance(message.get("item"), dict) else {}
        external_id = str(item.get("id") or message.get("id") or "")
        if not msg_type or not external_id:
            return

        connection.execute(
            """
            INSERT INTO messages(type, external_id, discovered_at, is_deleted, sort_order)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(type, external_id) DO UPDATE SET
                discovered_at=excluded.discovered_at,
                is_deleted=excluded.is_deleted,
                sort_order=excluded.sort_order
            """,
            (
                msg_type,
                external_id,
                str(message.get("discovered_at") or ""),
                int(bool(message.get("is_deleted", item.get("is_deleted", False)))),
                sort_order,
            ),
        )
        row = connection.execute(
            "SELECT message_id FROM messages WHERE type=? AND external_id=?",
            (msg_type, external_id),
        ).fetchone()
        message_id = int(row["message_id"])

        item_fields = dict(item)
        item_fields.pop("id", None)
        item_fields.pop("is_deleted", None)
        source_files = item_fields.pop("files", [])

        if msg_type == "announcement":
            item_fields["community_id"] = item_fields.pop(
                "com_id", item_fields.get("community_id", "")
            )
            self._save_typed_record(
                connection,
                "announcements",
                message_id,
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
            connection.execute(
                """
                INSERT INTO announcement_archives(message_id, attachments_cached)
                VALUES (?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    attachments_cached=excluded.attachments_cached
                """,
                (message_id, int(bool(message.get("attachments_cached", False)))),
            )
        elif msg_type == "reminder":
            self._save_typed_record(
                connection,
                "reminders",
                message_id,
                item_fields,
                ("pushing_time", "has_read", "title", "body", "feature_name", "target_id"),
            )
        elif msg_type == "personal":
            self._save_typed_record(
                connection,
                "personal_notifications",
                message_id,
                item_fields,
                ("community", "content", "push_date"),
            )
        else:
            return

        connection.execute("DELETE FROM message_attachments WHERE message_id=?", (message_id,))
        for purpose, attachments in (
            ("source", source_files),
            ("archive", message.get("attachments", [])),
        ):
            for index, attachment in enumerate(attachments or [], start=1):
                position = int(attachment.get("index", index))
                connection.execute(
                    """
                    INSERT INTO message_attachments(message_id, purpose, position, metadata_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (message_id, purpose, position, _json_dump(attachment)),
                )

    @staticmethod
    def _save_typed_record(
        connection: sqlite3.Connection,
        table: str,
        message_id: int,
        item: dict[str, Any],
        columns: tuple[str, ...],
    ) -> None:
        extra = {key: value for key, value in item.items() if key not in columns}
        defaults: dict[str, dict[str, Any]] = {
            "announcements": {
                "community_id": "",
                "title": "",
                "content": "",
                "create_date": "",
                "start_date": "",
                "end_date": "",
                "is_top": False,
                "type_name": "公告",
                "already_read": True,
            },
            "reminders": {
                "pushing_time": "",
                "has_read": True,
                "title": "",
                "body": "",
                "feature_name": "",
                "target_id": "",
            },
            "personal_notifications": {"community": "", "content": "", "push_date": ""},
        }
        table_defaults = defaults[table]
        values = [
            table_defaults.get(column) if item.get(column) is None else item.get(column)
            for column in columns
        ]
        if table == "announcements":
            values[6] = int(bool(values[6]))
            values[8] = str(values[8] or "公告")
            values[9] = int(bool(values[9] if values[9] is not None else True))
        elif table == "reminders":
            values[1] = int(bool(values[1] if values[1] is not None else True))

        column_list = ", ".join(("message_id", *columns, "extra_json"))
        placeholders = ", ".join("?" for _ in range(len(columns) + 2))
        update_fields = ", ".join(f"{column}=excluded.{column}" for column in columns)
        connection.execute(
            f"INSERT INTO {table}({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(message_id) DO UPDATE SET {update_fields}, "
            "extra_json=excluded.extra_json",
            (message_id, *values, _json_dump(extra)),
        )

    @staticmethod
    def _save_device(
        connection: sqlite3.Connection, sub: dict[str, Any], device_id: str | None = None
    ) -> str:
        endpoint = str(sub.get("endpoint") or "")
        if not endpoint:
            return ""
        device_id = str(device_id or sub.get("device_id") or _device_id(endpoint))
        keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
        connection.execute(
            """
            INSERT INTO devices(
                device_id, endpoint, p256dh, auth, user_agent, subscribed_at, create_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET
                endpoint=excluded.endpoint, p256dh=excluded.p256dh, auth=excluded.auth,
                user_agent=excluded.user_agent, subscribed_at=excluded.subscribed_at,
                create_time=excluded.create_time
            """,
            (
                device_id,
                endpoint,
                str(keys.get("p256dh") or ""),
                str(keys.get("auth") or ""),
                str(sub.get("user_agent") or ""),
                str(sub.get("subscribed_at") or ""),
                str(sub.get("create_time") or ""),
            ),
        )
        return device_id

    @staticmethod
    def _record_legacy_delivery(connection: sqlite3.Connection, device_id: str, uid: str) -> None:
        msg_type, separator, external_id = uid.partition(":")
        if not separator:
            return
        row = connection.execute(
            "SELECT message_id FROM messages WHERE type=? AND external_id=?",
            (msg_type, external_id),
        ).fetchone()
        if row:
            connection.execute(
                """
                INSERT OR IGNORE INTO push_deliveries(
                    device_id, message_id, status, attempts
                ) VALUES (?, ?, 'sent', 0)
                """,
                (device_id, int(row["message_id"])),
            )

    def load_message_store(self) -> dict[str, Any]:
        with self._connection() as connection:
            messages: list[dict[str, Any]] = []
            rows = connection.execute(
                "SELECT * FROM messages ORDER BY sort_order ASC, message_id ASC"
            ).fetchall()
            for row in rows:
                messages.append(self._load_message(connection, row))
            initialized = [
                row["type"]
                for row in connection.execute(
                    "SELECT type FROM initialized_types ORDER BY rowid"
                ).fetchall()
            ]
            state = connection.execute("SELECT * FROM sync_state WHERE singleton_id=1").fetchone()
            metadata = _json_load(state["extra_json"], {}) if state else {}
            if state:
                for key in ("community_name", "com_id", "last_synced_at", "last_full_sync_at"):
                    if state[key]:
                        metadata[key] = state[key]
        return {"initialized_types": initialized, "messages": messages, "metadata": metadata}

    @staticmethod
    def _load_message(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        msg_type = row["type"]
        item: dict[str, Any]
        if msg_type == "announcement":
            typed = connection.execute(
                "SELECT * FROM announcements WHERE message_id=?", (row["message_id"],)
            ).fetchone()
            item = _json_load(typed["extra_json"], {}) if typed else {}
            if typed:
                item.update(
                    {
                        "com_id": typed["community_id"],
                        "title": typed["title"],
                        "content": typed["content"],
                        "create_date": typed["create_date"],
                        "start_date": typed["start_date"],
                        "end_date": typed["end_date"],
                        "is_top": bool(typed["is_top"]),
                        "type_id": typed["type_id"],
                        "type_name": typed["type_name"],
                        "already_read": bool(typed["already_read"]),
                    }
                )
        elif msg_type == "reminder":
            typed = connection.execute(
                "SELECT * FROM reminders WHERE message_id=?", (row["message_id"],)
            ).fetchone()
            item = _json_load(typed["extra_json"], {}) if typed else {}
            if typed:
                item.update(
                    {
                        "pushing_time": typed["pushing_time"],
                        "has_read": bool(typed["has_read"]),
                        "title": typed["title"],
                        "body": typed["body"],
                        "feature_name": typed["feature_name"],
                        "target_id": typed["target_id"],
                    }
                )
        elif msg_type == "personal":
            typed = connection.execute(
                "SELECT * FROM personal_notifications WHERE message_id=?",
                (row["message_id"],),
            ).fetchone()
            item = _json_load(typed["extra_json"], {}) if typed else {}
            if typed:
                item.update(
                    {
                        "community": typed["community"],
                        "content": typed["content"],
                        "push_date": typed["push_date"],
                    }
                )
        else:
            item = {}

        item["id"] = row["external_id"]
        item["is_deleted"] = bool(row["is_deleted"])
        source_attachments = connection.execute(
            """
            SELECT metadata_json FROM message_attachments
            WHERE message_id=? AND purpose='source' ORDER BY position
            """,
            (row["message_id"],),
        ).fetchall()
        if source_attachments or msg_type in ("reminder", "personal"):
            item["files"] = [_json_load(att["metadata_json"], {}) for att in source_attachments]

        message = {
            "uid": f"{msg_type}:{row['external_id']}",
            "type": msg_type,
            "id": row["external_id"],
            "discovered_at": row["discovered_at"],
            "is_deleted": bool(row["is_deleted"]),
            "item": item,
        }
        archive_attachments = connection.execute(
            """
            SELECT metadata_json FROM message_attachments
            WHERE message_id=? AND purpose='archive' ORDER BY position
            """,
            (row["message_id"],),
        ).fetchall()
        if archive_attachments:
            message["attachments"] = [
                _json_load(att["metadata_json"], {}) for att in archive_attachments
            ]
        if msg_type == "announcement":
            archive = connection.execute(
                "SELECT attachments_cached FROM announcement_archives WHERE message_id=?",
                (row["message_id"],),
            ).fetchone()
            message["attachments_cached"] = (
                bool(archive["attachments_cached"]) if archive else False
            )
        return message

    def save_message_store(self, store: dict[str, Any]) -> None:
        with self._connection() as connection:
            for msg_type in dict.fromkeys(store.get("initialized_types", [])):
                connection.execute(
                    "INSERT OR IGNORE INTO initialized_types(type) VALUES (?)", (str(msg_type),)
                )
            for sort_order, message in enumerate(store.get("messages", [])):
                self._save_message(connection, message, sort_order)
            self._save_metadata(connection, store.get("metadata", {}))

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
            return self._load_device(connection, row) if row else None

    def list_devices(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM devices ORDER BY device_id").fetchall()
            return [self._load_device(connection, row) for row in rows]

    @staticmethod
    def _load_device(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        pushed = connection.execute(
            """
            SELECT m.type, m.external_id
            FROM push_deliveries d JOIN messages m ON m.message_id=d.message_id
            WHERE d.device_id=? AND d.status='sent'
            ORDER BY m.message_id
            """,
            (row["device_id"],),
        ).fetchall()
        return {
            "device_id": row["device_id"],
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
            "user_agent": row["user_agent"],
            "subscribed_at": row["subscribed_at"],
            "create_time": row["create_time"],
            "pushed_message_uids": [f"{r['type']}:{r['external_id']}" for r in pushed],
        }

    def save_device(self, sub: dict[str, Any]) -> None:
        with self._connection() as connection:
            device_id = self._save_device(connection, sub)
            if "pushed_message_uids" not in sub:
                return
            uids = list(dict.fromkeys(str(uid) for uid in sub.get("pushed_message_uids", [])))
            for uid in uids:
                self._record_legacy_delivery(connection, device_id, uid)

    def delete_device(self, device_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM devices WHERE device_id=?", (device_id,))

    def record_delivery(self, device_id: str, uid: str, status: str) -> None:
        msg_type, separator, external_id = uid.partition(":")
        if not separator:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT message_id FROM messages WHERE type=? AND external_id=?",
                (msg_type, external_id),
            ).fetchone()
            if not row:
                return
            connection.execute(
                """
                INSERT INTO push_deliveries(
                    device_id, message_id, status, attempts, last_attempt_at, sent_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(device_id, message_id) DO UPDATE SET
                    status=excluded.status,
                    attempts=push_deliveries.attempts+1,
                    last_attempt_at=excluded.last_attempt_at,
                    sent_at=CASE WHEN excluded.status='sent' THEN excluded.sent_at
                                 ELSE push_deliveries.sent_at END
                """,
                (
                    device_id,
                    int(row["message_id"]),
                    status,
                    now,
                    now if status == "sent" else "",
                ),
            )
