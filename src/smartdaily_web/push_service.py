"""VAPID Web Push Management, Central Message Store, and Per-Device Push Tracker."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)


def _make_device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


def _safe_int(val: Any) -> int | None:
    try:
        return int(str(val))
    except (ValueError, TypeError):
        return None


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomically write JSON data to path via a temporary file rename."""
    tmp_path = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


class PushService:
    """Manages VAPID keys, central message store, and per-device Web Push delivery."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        base_dir = Path(data_dir or os.getenv("DATA_DIR", "./data"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = base_dir
        self.devices_dir = self.data_dir / "devices"
        self.devices_dir.mkdir(parents=True, exist_ok=True)
        self.attachments_dir = self.data_dir / "attachments"
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.vapid_file = self.data_dir / "vapid_keys.json"
        self.messages_file = self.data_dir / "messages.json"
        self.legacy_subs_file = self.data_dir / "subscriptions.json"
        self.legacy_seen_file = self.data_dir / "seen_state.json"
        self.vapid_subject = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")
        self._store_lock = threading.RLock()
        self.public_key, self.private_key = self._load_or_create_vapid_keys()
        self._migrate_legacy_subscriptions()

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
    # Central Message Store (data/messages.json)
    # -------------------------------------------------------------------------
    def get_message_store(self) -> dict[str, Any]:
        """Read the central message store from messages.json."""
        if not self.messages_file.is_file():
            return {"initialized_types": [], "messages": []}
        try:
            data = json.loads(self.messages_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return {
                    "initialized_types": ["announcement", "personal", "reminder"],
                    "messages": data,
                }
            return {
                "initialized_types": list(data.get("initialized_types", [])),
                "messages": list(data.get("messages", [])),
            }
        except Exception:
            return {"initialized_types": [], "messages": []}

    def get_messages(self) -> list[dict[str, Any]]:
        """Return all stored messages (newest first)."""
        return self.get_message_store()["messages"]

    def save_message_store(self, store: dict[str, Any]) -> None:
        """Persist all messages to messages.json atomically without upper limit."""
        payload = {
            "initialized_types": list(dict.fromkeys(store.get("initialized_types", []))),
            "messages": list(store.get("messages", [])),
        }
        _atomic_write_json(self.messages_file, payload)

    # -------------------------------------------------------------------------
    # Per-Device Files (data/devices/<device_id>.json)
    # -------------------------------------------------------------------------
    def _device_file(self, device_id: str) -> Path:
        return self.devices_dir / f"{device_id}.json"

    def _migrate_legacy_subscriptions(self) -> None:
        """Migrate legacy data/subscriptions.json into per-device files in data/devices/."""
        if not self.legacy_subs_file.is_file():
            return
        try:
            raw_subs = json.loads(self.legacy_subs_file.read_text(encoding="utf-8"))
            if isinstance(raw_subs, list):
                for sub in raw_subs:
                    endpoint = sub.get("endpoint")
                    if not endpoint:
                        continue
                    device_id = sub.get("device_id") or _make_device_id(endpoint)
                    sub["device_id"] = device_id
                    dev_path = self._device_file(device_id)
                    if not dev_path.is_file():
                        _atomic_write_json(dev_path, sub)
            self.legacy_subs_file.unlink()
        except Exception as exc:
            logger.warning("Failed to migrate legacy subscriptions.json: %s", exc)

    def get_device_subscription(self, device_id: str) -> dict[str, Any] | None:
        """Read a single device's subscription and pushed state file."""
        dev_path = self._device_file(device_id)
        if not dev_path.is_file():
            return None
        try:
            sub = json.loads(dev_path.read_text(encoding="utf-8"))
            if "device_id" not in sub and sub.get("endpoint"):
                sub["device_id"] = device_id
            return sub
        except Exception:
            return None

    def save_device_subscription(self, sub: dict[str, Any]) -> None:
        """Atomically save a single device's subscription file."""
        endpoint = sub.get("endpoint", "")
        device_id = sub.get("device_id") or _make_device_id(endpoint)
        sub["device_id"] = device_id
        _atomic_write_json(self._device_file(device_id), sub)

    def delete_device_subscription(self, device_id: str) -> None:
        """Delete a single device's subscription file if it exists."""
        dev_path = self._device_file(device_id)
        if dev_path.is_file():
            try:
                dev_path.unlink()
            except Exception:
                pass

    def get_subscriptions(self) -> list[dict[str, Any]]:
        """Return all active device subscriptions from data/devices/*.json."""
        subs: list[dict[str, Any]] = []
        for path in sorted(self.devices_dir.glob("*.json")):
            try:
                sub = json.loads(path.read_text(encoding="utf-8"))
                if sub.get("endpoint"):
                    sub.setdefault("device_id", _make_device_id(sub["endpoint"]))
                    subs.append(sub)
            except Exception:
                continue
        return subs

    def add_subscription(self, subscription: dict[str, Any], user_agent: str = "") -> int:
        """
        Register or update a single device subscription in data/devices/<device_id>.json.
        New devices initialize pushed_message_uids with currently stored messages
        so they only receive messages arriving after subscription time.
        """
        endpoint = subscription.get("endpoint")
        if not endpoint:
            raise ValueError("Invalid push subscription: missing endpoint")

        with self._store_lock:
            device_id = _make_device_id(endpoint)
            existing = self.get_device_subscription(device_id)
            known_uids = [m["uid"] for m in self.get_messages() if m.get("uid")]

            if existing and "pushed_message_uids" in existing:
                pushed_uids = list(existing["pushed_message_uids"])
                subscribed_at = (
                    existing.get("subscribed_at") or datetime.now(timezone.utc).isoformat()
                )
            else:
                pushed_uids = list(known_uids)
                subscribed_at = datetime.now(timezone.utc).isoformat()

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
            return len(self.get_subscriptions())

    def remove_subscription(self, endpoint: str) -> int:
        """Remove a single device's subscription file without touching other devices."""
        device_id = _make_device_id(endpoint)
        self.delete_device_subscription(device_id)
        return len(self.get_subscriptions())

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
            status = self._send_single_push(sub, payload_dict)
            if status == "sent":
                sent += 1
                active_count += 1
            elif status == "expired":
                failed += 1
                self.delete_device_subscription(sub["device_id"])
            else:
                failed += 1
                active_count += 1

        return {"sent": sent, "failed": failed, "active_subscribers": active_count}

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
            rem_title = raw_item.get("title") or "新提醒通知"
            return {
                "uid": f"reminder:{item_id}",
                "type": "reminder",
                "id": item_id,
                "title": f"📬 我的提醒：{rem_title}",
                "body": (raw_item.get("body") or "點擊查看提醒內容")[:120],
                "url": "/?tab=reminders",
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
        page_size: int = 10,
    ) -> None:
        """
        Mark stored messages of msg_type as is_deleted=True if they are missing
        from the latest top-page response and were not merely pushed to page 2.
        """
        live_by_id = {str(it.get("id", "")): it for it in live_items if it.get("id")}
        has_more_pages = len(live_items) >= page_size

        # Compute minimum numeric ID on the current top page to distinguish pagination vs deletion
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
        else:
            all_ids = [
                _safe_int(it.get("id")) for it in live_items if _safe_int(it.get("id")) is not None
            ]
            cutoff_int_id = min(all_ids) if all_ids else None

        for msg in existing_messages:
            if msg.get("type") != msg_type:
                continue
            mid = str(msg.get("id", ""))
            if mid in live_by_id:
                msg["is_deleted"] = False
                old_content = (
                    msg["item"].get("content", "") if isinstance(msg.get("item"), dict) else ""
                )
                item_copy = dict(live_by_id[mid])
                if not item_copy.get("content") and old_content:
                    item_copy["content"] = old_content
                item_copy["is_deleted"] = False
                msg["item"] = item_copy
            else:
                msg.setdefault("is_deleted", False)
                if not has_more_pages:
                    msg["is_deleted"] = True
                elif cutoff_int_id is not None:
                    stored_int_id = _safe_int(mid)
                    if stored_int_id is not None and stored_int_id >= cutoff_int_id:
                        msg["is_deleted"] = True
                if isinstance(msg.get("item"), dict):
                    msg["item"]["is_deleted"] = bool(msg["is_deleted"])

    def sync_messages(
        self,
        announcements: list[dict[str, Any]] | None = None,
        personal_notifications: list[dict[str, Any]] | None = None,
        reminders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Record fetched announcements, personal notifications, and reminders into
        the central message store (data/messages.json) without upper limit, and
        update is_deleted flags for recalled messages.
        When a category is initialized for the first time, its current items are
        recorded into the message store AND marked as baseline in each existing
        device file so old historical messages do not trigger push floods.
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

            newly_added_chronological: list[dict[str, Any]] = []
            new_non_baseline_events: list[dict[str, Any]] = []
            baseline_uids: list[str] = []

            for msg_type, raw_list in inputs_by_type.items():
                if raw_list is None:
                    continue
                self._update_deleted_flags_for_type(existing_messages, msg_type, raw_list)
                is_type_initialized = msg_type in initialized_types

                for raw_item in reversed(raw_list):
                    candidate = self._build_candidate_record(msg_type, raw_item, now_iso)
                    if not candidate:
                        continue
                    uid = candidate["uid"]
                    if uid not in existing_uids:
                        existing_uids.add(uid)
                        newly_added_chronological.append(candidate)
                        if is_type_initialized:
                            new_non_baseline_events.append(candidate)
                        else:
                            baseline_uids.append(uid)
                    elif not is_type_initialized:
                        baseline_uids.append(uid)

                initialized_types.add(msg_type)

            # Ensure all existing messages have is_deleted boolean
            for m in existing_messages:
                m.setdefault("is_deleted", False)

            updated_messages = list(reversed(newly_added_chronological)) + existing_messages
            self.save_message_store(
                {
                    "initialized_types": list(initialized_types),
                    "messages": updated_messages,
                }
            )

            # Ensure each existing device file has baseline UIDs recorded independently
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

            return new_non_baseline_events

    def merge_with_deleted_items(
        self,
        msg_type: str,
        live_items: list[dict[str, Any]],
        is_first_page: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Sync first-page live items into messages.json and return a merged list
        containing live items (is_deleted=False) and recalled items (is_deleted=True).
        """
        if is_first_page:
            kwargs: dict[str, Any] = {
                "announcements": live_items if msg_type == "announcement" else None,
                "personal_notifications": live_items if msg_type == "personal" else None,
                "reminders": live_items if msg_type == "reminder" else None,
            }
            self.sync_messages(**kwargs)

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

        combined.sort(key=lambda x: _safe_int(x.get("id")) or 0, reverse=True)
        return combined

    def get_uncached_announcement_ids(self) -> list[str]:
        """Return announcement IDs in messages.json whose attachments are not yet cached."""
        with self._store_lock:
            return [
                str(m["id"])
                for m in self.get_messages()
                if m.get("type") == "announcement"
                and m.get("id")
                and not m.get("is_deleted")
                and not m.get("attachments_cached")
            ]

    def save_announcement_detail_and_attachments(
        self,
        announcement_id: str,
        detail: dict[str, Any],
        downloaded_files: dict[int, tuple[str, bytes]],
    ) -> list[dict[str, Any]]:
        """
        Save downloaded attachment binaries to data/attachments/<announcement_id>/
        and update the announcement record in data/messages.json with local attachment URLs.
        """
        safe_ann_id = str(announcement_id).strip()
        if not safe_ann_id or "/" in safe_ann_id or ".." in safe_ann_id:
            return detail.get("attachments", [])

        ann_dir = self.attachments_dir / safe_ann_id
        if downloaded_files:
            ann_dir.mkdir(parents=True, exist_ok=True)

        saved_attachments: list[dict[str, Any]] = []
        for att in detail.get("attachments", []):
            idx = int(att.get("index", 1))
            att_copy = dict(att)
            if idx in downloaded_files:
                local_name, file_bytes = downloaded_files[idx]
                file_path = ann_dir / local_name
                file_path.write_bytes(file_bytes)
                att_copy["url"] = f"/api/attachments/{safe_ann_id}/{local_name}"
                att_copy["local_file"] = local_name
            saved_attachments.append(att_copy)

        with self._store_lock:
            store = self.get_message_store()
            target_uid = f"announcement:{safe_ann_id}"
            found = next((m for m in store["messages"] if m.get("uid") == target_uid), None)
            if found:
                # Mark attachments_cached=True when all attachments are downloaded
                expected_count = len(detail.get("attachments", []))
                found["attachments_cached"] = len(downloaded_files) >= expected_count
                found["attachments"] = saved_attachments
                if detail.get("content"):
                    if not isinstance(found.get("item"), dict):
                        found["item"] = self._reconstruct_display_item(found)
                    found["item"]["content"] = detail["content"]
                self.save_message_store(store)

        return saved_attachments

    def get_attachment_file_path(self, announcement_id: str, filename: str) -> Path | None:
        """Resolve and validate a local attachment path under data/attachments/."""
        if (
            not announcement_id
            or not filename
            or "/" in announcement_id
            or "\\" in announcement_id
            or ".." in announcement_id
            or "/" in filename
            or "\\" in filename
            or ".." in filename
        ):
            return None
        candidate = (self.attachments_dir / announcement_id / filename).resolve()
        base_resolved = self.attachments_dir.resolve()
        if not str(candidate).startswith(str(base_resolved) + os.sep):
            return None
        return candidate if candidate.is_file() else None

    def get_stored_announcement_detail(
        self, announcement_id: str, mark_deleted: bool = False
    ) -> dict[str, Any] | None:
        """
        Retrieve an announcement from messages.json (including locally backed-up attachments).
        """
        with self._store_lock:
            store = self.get_message_store()
            target_uid = f"announcement:{announcement_id}"
            found = next((m for m in store["messages"] if m.get("uid") == target_uid), None)
            if not found:
                return None

            if mark_deleted and not found.get("is_deleted"):
                found["is_deleted"] = True
                if isinstance(found.get("item"), dict):
                    found["item"]["is_deleted"] = True
                self.save_message_store(store)

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
        For each subscribed device file in data/devices/<device_id>.json, find
        messages in data/messages.json not yet pushed to that device, send Web Push,
        and update only that device's file atomically.
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

            # Oldest unpushed first so notifications arrive in chronological order
            unpushed = [
                m for m in reversed(messages) if m.get("uid") and m["uid"] not in pushed_set
            ]

            device_expired = False
            newly_pushed_for_device: list[str] = []

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
                    newly_pushed_for_device.append(msg["uid"])
                    pushed_set.add(msg["uid"])
                elif status == "expired":
                    failed += 1
                    device_expired = True
                    self.delete_device_subscription(device_id)
                    break
                else:
                    failed += 1

            if not device_expired:
                active_count += 1
                if newly_pushed_for_device:
                    # Re-read device file in case it was updated or removed concurrently
                    current_on_disk = self.get_device_subscription(device_id)
                    if current_on_disk is not None:
                        disk_uids = list(current_on_disk.get("pushed_message_uids", []))
                        merged = list(
                            dict.fromkeys(list(reversed(newly_pushed_for_device)) + disk_uids)
                        )
                        current_on_disk["pushed_message_uids"] = merged
                        self.save_device_subscription(current_on_disk)

        return {
            "sent": sent,
            "failed": failed,
            "active_subscribers": active_count,
        }
