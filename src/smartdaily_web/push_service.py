"""VAPID Web Push Management, Central Message Store, and Per-Device Push Tracker."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

from .database import SQLiteStore

logger = logging.getLogger(__name__)

REMINDER_TYPE_LABELS: dict[str, str] = {
    "postal": "📦 郵務包裹",
    "Col": "🔑 寄放物",
    "Ann": "📢 社區公告",
    "CommunityPushNotification": "🔔 個人通知",
    "Msg": "🔔 個人通知",
    "Bill": "💳 帳單繳費",
    "payment": "💳 帳單繳費",
    "Gas": "🔥 瓦斯抄表",
    "Res": "📅 公設預約",
    "vote": "🗳️ 社區投票",
    "feedback": "💬 意見反映",
}


def get_reminder_type_label(feature_name: str) -> str:
    return REMINDER_TYPE_LABELS.get(str(feature_name or "").strip(), "📬 其他提醒")


def _make_device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


def _safe_int(val: Any) -> int | None:
    try:
        return int(str(val))
    except (ValueError, TypeError):
        return None


def _parse_iso_ts(val: Any) -> float | None:
    s = str(val or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "." in s:
            head, tail = s.split(".", 1)
            tz_sep = "+" if "+" in tail else ("-" if "-" in tail else "")
            if tz_sep:
                frac, tz = tail.split(tz_sep, 1)
                s = f"{head}.{frac[:6].ljust(6, '0')}{tz_sep}{tz}"
            else:
                s = f"{head}.{tail[:6].ljust(6, '0')}"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _message_is_pushable_for_device(msg: dict[str, Any], sub: dict[str, Any]) -> bool:
    """Ignore messages already stored before device creation and older reminders."""
    cutoff = _parse_iso_ts(sub.get("create_time") or sub.get("subscribed_at"))
    if cutoff is None:
        return True

    discovered_at = _parse_iso_ts(msg.get("discovered_at"))
    if discovered_at is not None and discovered_at < cutoff:
        return False

    item = msg.get("item") if isinstance(msg.get("item"), dict) else {}
    message_time = _parse_iso_ts(item.get("pushing_time"))
    return message_time is None or message_time >= cutoff


class PushService:
    """Manages VAPID keys, central message store, and per-device Web Push delivery."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        base_dir = Path(data_dir or os.getenv("DATA_DIR", "./data"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = base_dir
        self.attachments_dir = self.data_dir / "attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.vapid_file = self.data_dir / "vapid_keys.json"
        self.legacy_seen_file = self.data_dir / "seen_state.json"
        self.vapid_subject = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")
        self._store_lock = threading.RLock()
        self.database = SQLiteStore(self.data_dir)
        self.database_file = self.database.database_file
        self.public_key, self.private_key = self._load_or_create_vapid_keys()
        self.migrate()

    def migrate(self) -> None:
        """Apply the database schema and one-time device or attachment migrations."""
        self.database.migrate()

    def remove_legacy_messages_file_after_full_sync(self) -> bool:
        """Remove the old message export after a complete sync has been saved."""
        return self.database.remove_legacy_messages_file_after_full_sync()

    def _load_or_create_vapid_keys(self) -> tuple[str, str]:
        """Load VAPID keys from env or disk, or generate a new EC P-256 keypair."""
        env_pub = os.getenv("VAPID_PUBLIC_KEY", "").strip()
        env_priv = os.getenv("VAPID_PRIVATE_KEY", "").strip()
        if env_pub and env_priv:
            return env_pub, env_priv

        if self.vapid_file.is_file():
            try:
                data = json.loads(self.vapid_file.read_text(encoding="utf-8"))
                if data.get("public_key") and data.get("private_key"):
                    return data["public_key"], data["private_key"]
            except Exception as exc:
                logger.warning("Failed to read vapid_keys.json, regenerating: %s", exc)

        v = Vapid()
        v.generate_keys()
        raw_pub = v.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        raw_priv = v.private_key.private_numbers().private_value.to_bytes(32, "big")
        pub_b64 = b64urlencode(raw_pub)
        priv_b64 = b64urlencode(raw_priv)

        self.vapid_file.write_text(
            json.dumps({"public_key": pub_b64, "private_key": priv_b64}, indent=2),
            encoding="utf-8",
        )
        return pub_b64, priv_b64

    # -------------------------------------------------------------------------
    # Central Message Store (SQLite)
    # -------------------------------------------------------------------------
    def get_message_store(self) -> dict[str, Any]:
        """Read database records in the message shape used by the application logic."""
        store = self.database.load_message_store()
        for message in store["messages"]:
            candidate = self._build_candidate_record(
                message["type"], message.get("item", {}), message.get("discovered_at", "")
            )
            if candidate:
                message.update({key: candidate[key] for key in ("title", "body", "url", "tag")})
                if message["type"] == "reminder":
                    message["feature_name"] = candidate["feature_name"]
        return store

    def get_messages(self) -> list[dict[str, Any]]:
        """Return all stored messages (newest first)."""
        return self.get_message_store()["messages"]

    def save_message_store(self, store: dict[str, Any]) -> None:
        """Persist messages and sync metadata in one SQLite transaction."""
        self.database.save_message_store(store)

    def get_store_metadata(self) -> dict[str, Any]:
        """Return background sync metadata persisted alongside messages."""
        return self.get_message_store().get("metadata", {})

    def update_store_metadata(self, **values: Any) -> None:
        """Update metadata while preserving messages and initialization state."""
        with self._store_lock:
            store = self.get_message_store()
            store["metadata"].update(values)
            self.save_message_store(store)

    def get_message_page(
        self, msg_type: str, offset: int = 0, max_count: int = 10
    ) -> tuple[list[dict[str, Any]], int]:
        """Read a sorted page of display items from SQLite."""
        with self._store_lock:
            records = [m for m in self.get_messages() if m.get("type") == msg_type]
            items = [self._reconstruct_display_item(m) for m in records]

            if msg_type == "announcement":
                items.sort(
                    key=lambda item: (
                        bool(item.get("is_top")),
                        _safe_int(item.get("id")) or 0,
                    ),
                    reverse=True,
                )
            else:
                items.sort(
                    key=lambda item: (
                        _parse_iso_ts(item.get("pushing_time")) or 0.0,
                        _safe_int(item.get("id")) or 0,
                    ),
                    reverse=True,
                )

            total = len(items)
            start = max(0, offset)
            count = max(1, min(max_count, 100))
            return items[start : start + count], total

    # -------------------------------------------------------------------------
    # Per-Device SQLite records
    # -------------------------------------------------------------------------
    def get_device_subscription(self, device_id: str) -> dict[str, Any] | None:
        """Read a device subscription and its sent-message projection from SQLite."""
        return self.database.get_device(device_id)

    def save_device_subscription(self, sub: dict[str, Any]) -> None:
        """Save a subscription and synchronize its legacy sent-UID projection."""
        self.database.save_device(sub)

    def delete_device_subscription(self, device_id: str) -> None:
        """Delete one device; SQLite cascades its delivery records."""
        self.database.delete_device(device_id)

    def get_subscriptions(self) -> list[dict[str, Any]]:
        """Return all active device subscriptions from SQLite."""
        return self.database.list_devices()

    def add_subscription(self, subscription: dict[str, Any], user_agent: str = "") -> int:
        """
        Register or update one device subscription in SQLite.
        New devices use message timestamps to skip history and only baseline messages
        whose discovery time is unknown.
        """
        endpoint = subscription.get("endpoint")
        if not endpoint:
            raise ValueError("Invalid push subscription: missing endpoint")

        with self._store_lock:
            device_id = _make_device_id(endpoint)
            existing = self.get_device_subscription(device_id)
            now = datetime.now(timezone.utc).isoformat()
            messages = self.get_messages()
            unknown_discovery_uids = [
                m["uid"]
                for m in messages
                if m.get("uid") and _parse_iso_ts(m.get("discovered_at")) is None
            ]

            if existing and "pushed_message_uids" in existing:
                pushed_uids = list(existing["pushed_message_uids"])
                subscribed_at = existing.get("subscribed_at") or existing.get("create_time") or now
            else:
                pushed_uids = unknown_discovery_uids
                subscribed_at = now

            self.save_device_subscription(
                {
                    "device_id": device_id,
                    "endpoint": endpoint,
                    "keys": subscription.get("keys", {}),
                    "user_agent": user_agent,
                    "subscribed_at": subscribed_at,
                    "pushed_message_uids": pushed_uids,
                }
            )
            total = len(self.get_subscriptions())
            logger.info(
                "Device subscription saved: device_id=%s (active_subscribers=%d)",
                device_id,
                total,
            )
            return total

    def remove_subscription(self, endpoint: str) -> int:
        """Remove a single device's subscription file without touching other devices."""
        device_id = _make_device_id(endpoint)
        self.delete_device_subscription(device_id)
        total = len(self.get_subscriptions())
        logger.info(
            "Device subscription removed: device_id=%s (active_subscribers=%d)",
            device_id,
            total,
        )
        return total

    # -------------------------------------------------------------------------
    # Message Sync & Per-Device Web Push Delivery
    # -------------------------------------------------------------------------
    def _send_single_push(self, sub: dict[str, Any], payload_dict: dict[str, str]) -> str:
        """
        Send a single Web Push payload to one device subscription.
        Returns 'sent', 'expired' (404/410), or 'failed'.
        """
        payload = json.dumps(
            {
                "title": payload_dict["title"],
                "body": payload_dict["body"],
                "url": payload_dict.get("url", "/"),
                "tag": payload_dict.get("tag", "smartdaily"),
            },
            ensure_ascii=False,
        )
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": sub["keys"],
                },
                data=payload,
                vapid_private_key=self.private_key,
                vapid_claims={"sub": self.vapid_subject},
                ttl=86400,
            )
            return "sent"
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code in (404, 410):
                logger.info(
                    "Removing expired push subscription (device=%s): %s",
                    sub.get("device_id", "?"),
                    sub["endpoint"][:50],
                )
                return "expired"
            logger.warning(
                "WebPush failed for device %s (%s): %s",
                sub.get("device_id", "?"),
                status_code,
                exc,
            )
            return "failed"
        except Exception as exc:
            logger.warning(
                "Unexpected push error for device %s: %s",
                sub.get("device_id", "?"),
                exc,
            )
            return "failed"

    def send_push_to_all(
        self,
        title: str,
        body: str,
        url: str = "/",
        tag: str = "smartdaily",
    ) -> dict[str, int]:
        """Send an immediate broadcast (e.g. test notification) to all active devices."""
        subs = self.get_subscriptions()
        payload_dict = {"title": title, "body": body, "url": url, "tag": tag}

        sent = 0
        failed = 0
        active_count = 0
        for sub in subs:
            device_id = sub.get("device_id", "?")
            logger.info("Sending broadcast push (%s) to device %s: %s", tag, device_id, title)
            status = self._send_single_push(sub, payload_dict)
            if status == "sent":
                sent += 1
                active_count += 1
                logger.info("Broadcast push delivered to device %s", device_id)
            elif status == "expired":
                failed += 1
                self.delete_device_subscription(sub["device_id"])
            else:
                failed += 1
                active_count += 1

        logger.info(
            "Broadcast push completed: sent=%d, failed=%d, active_subscribers=%d",
            sent,
            failed,
            active_count,
        )
        return {"sent": sent, "failed": failed, "active_subscribers": active_count}

    def send_push_to_device(
        self,
        endpoint: str,
        title: str,
        body: str,
        url: str = "/",
        tag: str = "smartdaily",
    ) -> dict[str, int] | None:
        """Send one immediate push to the registered device with this endpoint."""
        sub = next(
            (item for item in self.get_subscriptions() if item["endpoint"] == endpoint), None
        )
        if sub is None:
            return None

        payload_dict = {"title": title, "body": body, "url": url, "tag": tag}
        logger.info("Sending targeted push (%s) to current device %s", tag, sub["device_id"])
        status = self._send_single_push(sub, payload_dict)
        if status == "expired":
            self.delete_device_subscription(sub["device_id"])
            return {"sent": 0, "failed": 1, "active_subscribers": 0}
        if status == "failed":
            return {"sent": 0, "failed": 1, "active_subscribers": 1}
        return {"sent": 1, "failed": 0, "active_subscribers": 1}

    @staticmethod
    def _build_candidate_record(
        msg_type: str, raw_item: dict[str, Any], now_iso: str
    ) -> dict[str, Any] | None:
        item_id = str(raw_item.get("id", ""))
        if not item_id:
            return None
        item_copy = dict(raw_item)
        item_copy["is_deleted"] = False

        if msg_type == "announcement":
            return {
                "uid": f"announcement:{item_id}",
                "type": "announcement",
                "id": item_id,
                "title": f"📢 新社區公告：{raw_item.get('title', '')}",
                "body": (raw_item.get("content") or "點擊查看公告詳情與附件")[:120],
                "url": f"/?tab=announcements&id={item_id}",
                "tag": f"ann-{item_id}",
                "discovered_at": now_iso,
                "is_deleted": False,
                "item": item_copy,
            }
        if msg_type == "personal":
            return {
                "uid": f"personal:{item_id}",
                "type": "personal",
                "id": item_id,
                "title": f"🔔 社區個人通知（{raw_item.get('community', '物管中心')}）",
                "body": (raw_item.get("content") or "")[:120],
                "url": "/?tab=personal",
                "tag": f"notif-{item_id}",
                "discovered_at": now_iso,
                "is_deleted": False,
                "item": item_copy,
            }
        if msg_type == "reminder":
            feature = str(raw_item.get("feature_name") or "").strip()
            type_label = get_reminder_type_label(feature)
            emoji, separator, category = type_label.partition(" ")
            rem_title = str(raw_item.get("title") or "").strip() or "新提醒通知"
            target_id = str(raw_item.get("target_id") or "").strip()
            target_url = (
                f"/?tab=announcements&id={target_id}"
                if (feature == "Ann" and target_id)
                else "/?tab=reminders"
            )
            return {
                "uid": f"reminder:{item_id}",
                "type": "reminder",
                "id": item_id,
                "feature_name": feature,
                "title": f"{emoji} [{category if separator else type_label}] {rem_title}",
                "body": (raw_item.get("body") or "點擊查看提醒內容")[:120],
                "url": target_url,
                "tag": f"rem-{item_id}",
                "discovered_at": now_iso,
                "is_deleted": False,
                "item": item_copy,
            }
        return None

    @staticmethod
    def _reconstruct_display_item(msg: dict[str, Any]) -> dict[str, Any]:
        """Reconstruct a frontend-ready item dict from a stored message record."""
        is_deleted = bool(msg.get("is_deleted", False))
        if isinstance(msg.get("item"), dict):
            item = dict(msg["item"])
            item["is_deleted"] = is_deleted
            return item

        msg_type = msg.get("type", "")
        raw_title = str(msg.get("title", ""))
        for prefix in ("📢 新社區公告：", "📬 我的提醒："):
            if raw_title.startswith(prefix):
                raw_title = raw_title[len(prefix) :]
        if msg_type == "announcement":
            return {
                "id": str(msg.get("id", "")),
                "title": raw_title,
                "content": msg.get("body", ""),
                "create_date": msg.get("discovered_at", "")[:16].replace("T", " "),
                "start_date": "",
                "is_top": False,
                "type_name": "公告",
                "is_deleted": is_deleted,
            }
        if msg_type == "personal":
            return {
                "id": str(msg.get("id", "")),
                "community": "物管中心",
                "content": msg.get("body", ""),
                "push_date": msg.get("discovered_at", "")[:16].replace("T", " "),
                "files": [],
                "is_deleted": is_deleted,
            }
        return {
            "id": str(msg.get("id", "")),
            "title": raw_title,
            "body": msg.get("body", ""),
            "pushing_time": msg.get("discovered_at", ""),
            "has_read": True,
            "feature_name": "提醒",
            "target_id": "",
            "files": [],
            "is_deleted": is_deleted,
        }

    @staticmethod
    def _update_deleted_flags_for_type(
        existing_messages: list[dict[str, Any]],
        msg_type: str,
        live_items: list[dict[str, Any]],
        full_sync: bool = False,
    ) -> None:
        """
        Mark stored messages of msg_type as is_deleted=True if they are missing
        from the latest response. Partial syncs use the first-page cutoff to avoid
        marking older, unseen messages as recalled; full syncs can mark any missing
        stored message as recalled.
        """
        live_by_id = {str(it.get("id", "")): it for it in live_items if it.get("id")}

        # Compute minimum numeric ID or timestamp on current top page to distinguish pagination
        if msg_type == "announcement":
            non_top_ids = [
                _safe_int(it.get("id"))
                for it in live_items
                if not it.get("is_top") and _safe_int(it.get("id")) is not None
            ]
            all_ids = [
                _safe_int(it.get("id")) for it in live_items if _safe_int(it.get("id")) is not None
            ]
            cutoff_int_id = min(non_top_ids) if non_top_ids else (min(all_ids) if all_ids else None)
            cutoff_ts = None
        else:
            all_ids = [
                _safe_int(it.get("id")) for it in live_items if _safe_int(it.get("id")) is not None
            ]
            cutoff_int_id = min(all_ids) if all_ids else None
            all_ts = [
                _parse_iso_ts(it.get("pushing_time"))
                for it in live_items
                if _parse_iso_ts(it.get("pushing_time")) is not None
            ]
            cutoff_ts = min(all_ts) if all_ts else None

        for msg in existing_messages:
            if msg.get("type") != msg_type:
                continue
            mid = str(msg.get("id", ""))
            if mid in live_by_id:
                msg["is_deleted"] = False
                old_item = msg["item"] if isinstance(msg.get("item"), dict) else {}
                old_content = old_item.get("content", "")
                old_files = old_item.get("files", [])
                item_copy = dict(live_by_id[mid])
                if not item_copy.get("content") and old_content:
                    item_copy["content"] = old_content
                if not item_copy.get("files") and old_files:
                    item_copy["files"] = old_files
                item_copy["is_deleted"] = False
                msg["item"] = item_copy
            else:
                was_deleted = bool(msg.get("is_deleted", False))
                msg.setdefault("is_deleted", False)
                if full_sync:
                    msg["is_deleted"] = True
                else:
                    stored_int_id = _safe_int(mid)
                    stored_item = msg.get("item") if isinstance(msg.get("item"), dict) else {}
                    stored_ts = _parse_iso_ts(stored_item.get("pushing_time"))
                    if (
                        cutoff_int_id is not None
                        and stored_int_id is not None
                        and stored_int_id >= cutoff_int_id
                    ):
                        msg["is_deleted"] = True
                    elif cutoff_ts is not None and stored_ts is not None and stored_ts >= cutoff_ts:
                        msg["is_deleted"] = True
                if not was_deleted and msg.get("is_deleted"):
                    logger.info(
                        "Message marked as recalled (is_deleted=True): %s (%s) %s",
                        msg_type,
                        mid,
                        msg.get("title", ""),
                    )
                if isinstance(msg.get("item"), dict):
                    msg["item"]["is_deleted"] = bool(msg["is_deleted"])

    def sync_messages(
        self,
        announcements: list[dict[str, Any]] | None = None,
        personal_notifications: list[dict[str, Any]] | None = None,
        reminders: list[dict[str, Any]] | None = None,
        full_sync: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Record fetched announcements, personal notifications, and reminders into
        the central message store without an upper limit, and
        update is_deleted flags for recalled messages.
        When a category is initialized for the first time, or when older historical
        messages from subsequent pages are backfilled, they are recorded into the
        message store and marked as baseline for each existing device so old
        historical messages do not trigger push floods.
        Returns the list of newly discovered non-baseline messages.
        """
        with self._store_lock:
            now_iso = datetime.now(timezone.utc).isoformat()
            store = self.get_message_store()
            initialized_types = set(store["initialized_types"])
            existing_messages = store["messages"]
            existing_uids = {m["uid"] for m in existing_messages if m.get("uid")}

            inputs_by_type: dict[str, list[dict[str, Any]] | None] = {
                "announcement": announcements,
                "personal": personal_notifications,
                "reminder": reminders,
            }

            newly_added_newer: list[dict[str, Any]] = []
            newly_added_older: list[dict[str, Any]] = []
            new_non_baseline_events_newest_first: list[dict[str, Any]] = []
            baseline_uids: list[str] = []

            for msg_type, raw_list in inputs_by_type.items():
                if raw_list is None:
                    continue
                self._update_deleted_flags_for_type(
                    existing_messages, msg_type, raw_list, full_sync=full_sync
                )
                is_type_initialized = msg_type in initialized_types

                existing_type_msgs = [m for m in existing_messages if m.get("type") == msg_type]
                valid_existing_ts = [
                    ts
                    for m in existing_type_msgs
                    if (ts := _parse_iso_ts((m.get("item") or {}).get("pushing_time"))) is not None
                ]
                max_existing_ts = max(valid_existing_ts) if valid_existing_ts else None
                seen_existing_for_type = False

                for raw_item in raw_list:
                    candidate = self._build_candidate_record(msg_type, raw_item, now_iso)
                    if not candidate:
                        continue
                    uid = candidate["uid"]
                    if uid not in existing_uids:
                        existing_uids.add(uid)
                        if not is_type_initialized:
                            newly_added_newer.append(candidate)
                            baseline_uids.append(uid)
                        else:
                            cand_ts = _parse_iso_ts(raw_item.get("pushing_time"))
                            if cand_ts is not None and max_existing_ts is not None:
                                is_newer = cand_ts > max_existing_ts or (
                                    cand_ts == max_existing_ts and not seen_existing_for_type
                                )
                            else:
                                is_newer = not seen_existing_for_type

                            if is_newer:
                                newly_added_newer.append(candidate)
                                if msg_type == "reminder":
                                    new_non_baseline_events_newest_first.append(candidate)
                                else:
                                    baseline_uids.append(uid)
                            else:
                                newly_added_older.append(candidate)
                                baseline_uids.append(uid)
                    else:
                        seen_existing_for_type = True
                        if not is_type_initialized or msg_type != "reminder":
                            baseline_uids.append(uid)

                initialized_types.add(msg_type)

            # Ensure all existing messages have is_deleted boolean
            for m in existing_messages:
                m.setdefault("is_deleted", False)

            updated_messages = newly_added_newer + existing_messages + newly_added_older
            self.save_message_store(
                {
                    "initialized_types": list(initialized_types),
                    "messages": updated_messages,
                    "metadata": store.get("metadata", {}),
                }
            )

            # Ensure each existing device has baseline UIDs recorded independently
            all_current_uids = [m["uid"] for m in updated_messages]
            for sub in self.get_subscriptions():
                device_id = sub["device_id"]
                latest_sub = self.get_device_subscription(device_id)
                if not latest_sub:
                    continue
                if "pushed_message_uids" not in latest_sub:
                    latest_sub["pushed_message_uids"] = list(all_current_uids)
                    self.save_device_subscription(latest_sub)
                elif baseline_uids:
                    merged = list(dict.fromkeys(baseline_uids + latest_sub["pushed_message_uids"]))
                    latest_sub["pushed_message_uids"] = merged
                    self.save_device_subscription(latest_sub)

            # Clean up obsolete seen_state.json if present
            if self.legacy_seen_file.is_file():
                try:
                    self.legacy_seen_file.unlink()
                except Exception:
                    pass

            return list(reversed(new_non_baseline_events_newest_first))

    def merge_with_deleted_items(
        self,
        msg_type: str,
        live_items: list[dict[str, Any]],
        is_first_page: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Read-only merge of live items (is_deleted=False) with recalled items
        (is_deleted=True) stored in SQLite. Does not write during reads.
        """
        live_ids = {str(it.get("id", "")) for it in live_items if it.get("id")}
        result_items = [dict(it, is_deleted=False) for it in live_items]

        if not is_first_page:
            return result_items

        deleted_items = [
            self._reconstruct_display_item(m)
            for m in self.get_messages()
            if m.get("type") == msg_type
            and m.get("is_deleted") is True
            and str(m.get("id", "")) not in live_ids
        ]
        if not deleted_items:
            return result_items

        combined = result_items + deleted_items
        if msg_type == "announcement":
            top_items = [i for i in combined if i.get("is_top")]
            non_top_items = [i for i in combined if not i.get("is_top")]
            non_top_items.sort(key=lambda x: _safe_int(x.get("id")) or 0, reverse=True)
            return top_items + non_top_items

        combined.sort(
            key=lambda x: (
                _parse_iso_ts(x.get("pushing_time")) or 0.0,
                _safe_int(x.get("id")) or 0,
            ),
            reverse=True,
        )
        return combined

    def get_uncached_announcement_ids(self) -> list[str]:
        """Return announcement IDs whose attachments are not yet cached."""
        with self._store_lock:
            return [
                str(m["id"])
                for m in self.get_messages()
                if m.get("type") == "announcement"
                and m.get("id")
                and not m.get("is_deleted")
                and not m.get("attachments_cached")
            ]

    def mark_announcement_deleted(self, announcement_id: str) -> None:
        """Mark an announcement recalled during a background detail fetch."""
        with self._store_lock:
            store = self.get_message_store()
            target_uid = f"announcement:{announcement_id}"
            found = next((m for m in store["messages"] if m.get("uid") == target_uid), None)
            if not found or found.get("is_deleted"):
                return
            found["is_deleted"] = True
            if isinstance(found.get("item"), dict):
                found["item"]["is_deleted"] = True
            self.save_message_store(store)
            logger.info(
                "Message marked as recalled (is_deleted=True): announcement (%s) %s",
                announcement_id,
                found.get("title", ""),
            )

    def save_announcement_detail_and_attachments(
        self,
        announcement_id: str,
        detail: dict[str, Any],
        downloaded_files: dict[int, tuple[str, bytes]],
    ) -> list[dict[str, Any]]:
        """
        Save downloaded files flat under data/attachments using UUID names.
        The database keeps each original filename and its UUID file name.
        """
        safe_ann_id = str(announcement_id).strip()
        if not safe_ann_id or "/" in safe_ann_id or ".." in safe_ann_id:
            return detail.get("attachments", [])

        saved_attachments: list[dict[str, Any]] = []
        created_files: list[Path] = []
        try:
            with self._store_lock:
                store = self.get_message_store()
                target_uid = f"announcement:{safe_ann_id}"
                found = next((m for m in store["messages"] if m.get("uid") == target_uid), None)
                if not found:
                    return detail.get("attachments", [])

                for att in detail.get("attachments", []):
                    idx = int(att.get("index", 1))
                    att_copy = dict(att)
                    if idx in downloaded_files:
                        local_name, file_bytes = downloaded_files[idx]
                        original_name = str(
                            att_copy.get("original_name")
                            or att_copy.get("filename")
                            or att_copy.get("name")
                            or local_name
                        )
                        stored_filename = str(uuid.uuid4())
                        file_path = self.attachments_dir / stored_filename
                        file_path.write_bytes(file_bytes)
                        created_files.append(file_path)
                        mime_type = str(
                            att_copy.get("mime_type")
                            or mimetypes.guess_type(original_name)[0]
                            or ""
                        )
                        att_copy.update(
                            {
                                "filename": original_name,
                                "original_name": original_name,
                                "stored_filename": stored_filename,
                                "local_file": stored_filename,
                                "mime_type": mime_type,
                                "url": f"/attachments/{stored_filename}",
                            }
                        )
                    saved_attachments.append(att_copy)

                found["attachments_cached"] = len(downloaded_files) >= len(
                    detail.get("attachments", [])
                )
                found["attachments"] = saved_attachments
                if detail.get("content"):
                    if not isinstance(found.get("item"), dict):
                        found["item"] = self._reconstruct_display_item(found)
                    found["item"]["content"] = detail["content"]
                self.save_message_store(store)
        except Exception:
            for file_path in created_files:
                file_path.unlink(missing_ok=True)
            raise

        return saved_attachments

    def get_attachment_metadata(self, stored_filename: str) -> dict[str, str] | None:
        """Return an attachment's original name and MIME type from SQLite."""
        return self.database.get_attachment_metadata(stored_filename)

    def get_attachment_file_path(self, stored_filename: str) -> Path | None:
        """Resolve an attachment UUID only when it has a matching SQLite record."""
        if not self.get_attachment_metadata(stored_filename):
            return None
        try:
            canonical_name = str(uuid.UUID(stored_filename))
        except (ValueError, TypeError, AttributeError):
            return None
        candidate = (self.attachments_dir / canonical_name).resolve()
        base_resolved = self.attachments_dir.resolve()
        if candidate.parent != base_resolved:
            return None
        return candidate if candidate.is_file() else None

    def get_stored_announcement_detail(self, announcement_id: str) -> dict[str, Any] | None:
        """
        Retrieve an announcement from SQLite (including locally backed-up attachments).
        """
        with self._store_lock:
            store = self.get_message_store()
            target_uid = f"announcement:{announcement_id}"
            found = next((m for m in store["messages"] if m.get("uid") == target_uid), None)
            if not found:
                return None

            disp = self._reconstruct_display_item(found)
            attachments = list(found.get("attachments") or [])
            return {
                "id": str(disp.get("id", announcement_id)),
                "title": disp.get("title", ""),
                "type_name": disp.get("type_name", "公告"),
                "create_date": disp.get("create_date") or disp.get("start_date") or "",
                "content": disp.get("content", ""),
                "attachments": attachments,
                "attachment_count": len(attachments),
                "attachments_cached": bool(found.get("attachments_cached", False)),
                "is_deleted": bool(found.get("is_deleted", False)),
            }

    def get_announcement_reminder_fallback(self, announcement_id: str) -> dict[str, Any] | None:
        """Return the saved reminder text when its linked announcement is not archived."""
        with self._store_lock:
            store = self.get_message_store()
            found = next(
                (
                    msg
                    for msg in store["messages"]
                    if msg.get("type") == "reminder"
                    and isinstance(msg.get("item"), dict)
                    and str(msg["item"].get("feature_name", "")).strip() == "Ann"
                    and str(msg["item"].get("target_id", "")) == str(announcement_id)
                ),
                None,
            )
            if not found:
                return None

            item = found["item"]
            return {
                "id": str(announcement_id),
                "title": str(item.get("title") or "社區公告通知"),
                "type_name": "公告摘要",
                "create_date": str(item.get("pushing_time") or ""),
                "content": str(item.get("body") or ""),
                "attachments": [],
                "attachment_count": 0,
                "attachments_cached": False,
                "is_deleted": False,
                "detail_unavailable": True,
            }

    def check_and_record_new_items(
        self,
        announcements: list[dict[str, Any]],
        personal_notifications: list[dict[str, Any]],
        reminders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Alias for sync_messages()."""
        return self.sync_messages(
            announcements=announcements,
            personal_notifications=personal_notifications,
            reminders=reminders,
        )

    def push_unpushed_messages(self) -> dict[str, Any]:
        """
        Find messages not yet pushed to each device, send Web Push, and record
        delivery attempts in SQLite.
        """
        messages = self.get_messages()
        subs = self.get_subscriptions()
        if not subs or not messages:
            return {"sent": 0, "failed": 0, "active_subscribers": len(subs)}

        all_current_uids = [m["uid"] for m in messages if m.get("uid")]
        sent = 0
        failed = 0
        active_count = 0

        for sub in subs:
            device_id = sub["device_id"]
            latest_sub = self.get_device_subscription(device_id)
            if not latest_sub:
                continue

            if "pushed_message_uids" not in latest_sub:
                latest_sub["pushed_message_uids"] = list(all_current_uids)
                self.save_device_subscription(latest_sub)

            pushed_uids: list[str] = list(latest_sub.get("pushed_message_uids", []))
            pushed_set = set(pushed_uids)

            # Oldest unpushed reminder first so notifications arrive in chronological order
            unpushed = [
                m
                for m in reversed(messages)
                if m.get("type") == "reminder"
                and not m.get("is_deleted")
                and m.get("uid")
                and m["uid"] not in pushed_set
                and _message_is_pushable_for_device(m, latest_sub)
            ]

            device_expired = False

            for msg in unpushed:
                logger.info(
                    "Pushing %s (%s) to device %s: %s",
                    msg["type"],
                    msg["id"],
                    device_id,
                    msg["title"],
                )
                status = self._send_single_push(latest_sub, msg)
                if status == "sent":
                    sent += 1
                    self.database.record_delivery(device_id, msg["uid"], "sent")
                    pushed_set.add(msg["uid"])
                    logger.info(
                        "Push delivered to device %s for %s (%s)",
                        device_id,
                        msg["type"],
                        msg["id"],
                    )
                elif status == "expired":
                    failed += 1
                    device_expired = True
                    self.delete_device_subscription(device_id)
                    break
                else:
                    failed += 1
                    self.database.record_delivery(device_id, msg["uid"], "failed")
                    logger.warning(
                        "Push delivery failed for device %s on %s (%s); will retry on next poll",
                        device_id,
                        msg["type"],
                        msg["id"],
                    )

            if not device_expired:
                active_count += 1

        return {
            "sent": sent,
            "failed": failed,
            "active_subscribers": active_count,
        }
