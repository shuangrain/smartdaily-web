"""VAPID Web Push Management, Central Message Store, and Per-Device Push Tracker."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)

MAX_STORED_MESSAGES = 500
MAX_DEVICE_PUSHED_UIDS = 500


def _make_device_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12]


class PushService:
    """Manages VAPID keys, central message store, and per-device Web Push delivery."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        base_dir = Path(data_dir or os.getenv("DATA_DIR", "./data"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = base_dir
        self.vapid_file = self.data_dir / "vapid_keys.json"
        self.subs_file = self.data_dir / "subscriptions.json"
        self.messages_file = self.data_dir / "messages.json"
        self.legacy_seen_file = self.data_dir / "seen_state.json"
        self.vapid_subject = os.getenv("VAPID_SUBJECT", "mailto:admin@example.com")
        self.public_key, self.private_key = self._load_or_create_vapid_keys()

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
        """Persist the central message store to messages.json."""
        payload = {
            "initialized_types": list(dict.fromkeys(store.get("initialized_types", []))),
            "messages": list(store.get("messages", []))[:MAX_STORED_MESSAGES],
        }
        self.messages_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # -------------------------------------------------------------------------
    # Subscriptions & Per-Device Pushed State (data/subscriptions.json)
    # -------------------------------------------------------------------------
    def get_subscriptions(self) -> list[dict[str, Any]]:
        if not self.subs_file.is_file():
            return []
        try:
            raw_subs = json.loads(self.subs_file.read_text(encoding="utf-8"))
            for sub in raw_subs:
                if "device_id" not in sub and sub.get("endpoint"):
                    sub["device_id"] = _make_device_id(sub["endpoint"])
            return raw_subs
        except Exception:
            return []

    def save_subscriptions(self, subs: list[dict[str, Any]]) -> None:
        self.subs_file.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_subscription(self, subscription: dict[str, Any], user_agent: str = "") -> int:
        """
        Register or update a device subscription.
        New devices initialize pushed_message_uids with currently stored messages
        so they only receive messages arriving after subscription time.
        """
        endpoint = subscription.get("endpoint")
        if not endpoint:
            raise ValueError("Invalid push subscription: missing endpoint")

        current_Subs = self.get_subscriptions()
        existing = next((s for s in current_Subs if s.get("endpoint") == endpoint), None)
        known_uids = [m["uid"] for m in self.get_messages() if m.get("uid")]

        if existing and "pushed_message_uids" in existing:
            pushed_uids = list(existing["pushed_message_uids"])
            subscribed_at = existing.get("subscribed_at") or datetime.now(timezone.utc).isoformat()
        else:
            pushed_uids = known_uids[:MAX_DEVICE_PUSHED_UIDS]
            subscribed_at = datetime.now(timezone.utc).isoformat()

        subs = [s for s in current_Subs if s.get("endpoint") != endpoint]
        subs.append(
            {
                "device_id": _make_device_id(endpoint),
                "endpoint": endpoint,
                "keys": subscription.get("keys", {}),
                "user_agent": user_agent,
                "subscribed_at": subscribed_at,
                "pushed_message_uids": pushed_uids,
            }
        )
        self.save_subscriptions(subs)
        return len(subs)

    def remove_subscription(self, endpoint: str) -> int:
        subs = [s for s in self.get_subscriptions() if s.get("endpoint") != endpoint]
        self.save_subscriptions(subs)
        return len(subs)

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
        valid_subs = []
        for sub in subs:
            status = self._send_single_push(sub, payload_dict)
            if status == "sent":
                sent += 1
                valid_subs.append(sub)
            elif status == "expired":
                failed += 1
            else:
                failed += 1
                valid_subs.append(sub)

        if len(valid_subs) != len(subs):
            self.save_subscriptions(valid_subs)

        return {"sent": sent, "failed": failed, "active_subscribers": len(valid_subs)}

    def sync_messages(
        self,
        announcements: list[dict[str, Any]],
        personal_notifications: list[dict[str, Any]],
        reminders: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Record fetched announcements, personal notifications, and reminders into
        the central message store (data/messages.json).
        When a category is initialized for the first time, its current items are
        recorded into the message store AND marked as baseline in all existing
        devices so old historical messages do not trigger push floods.
        Returns the list of newly discovered non-baseline messages.
        """
        reminders = reminders or []
        now_iso = datetime.now(timezone.utc).isoformat()
        store = self.get_message_store()
        initialized_types = set(store["initialized_types"])
        existing_messages = store["messages"]
        existing_uids = {m["uid"] for m in existing_messages if m.get("uid")}

        # Build normalized message candidates in chronological order (oldest -> newest)
        candidates_by_type: dict[str, list[dict[str, Any]]] = {
            "announcement": [],
            "personal": [],
            "reminder": [],
        }

        for ann in reversed(announcements):
            aid = str(ann.get("id", ""))
            if not aid:
                continue
            candidates_by_type["announcement"].append(
                {
                    "uid": f"announcement:{aid}",
                    "type": "announcement",
                    "id": aid,
                    "title": f"📢 新社區公告：{ann.get('title', '')}",
                    "body": (ann.get("content") or "點擊查看公告詳情與附件")[:120],
                    "url": f"/?tab=announcements&id={aid}",
                    "tag": f"ann-{aid}",
                    "discovered_at": now_iso,
                }
            )

        for notif in reversed(personal_notifications):
            nid = str(notif.get("id", ""))
            if not nid:
                continue
            candidates_by_type["personal"].append(
                {
                    "uid": f"personal:{nid}",
                    "type": "personal",
                    "id": nid,
                    "title": f"🔔 社區個人通知（{notif.get('community', '物管中心')}）",
                    "body": (notif.get("content") or "")[:120],
                    "url": "/?tab=personal",
                    "tag": f"notif-{nid}",
                    "discovered_at": now_iso,
                }
            )

        for rem in reversed(reminders):
            rid = str(rem.get("id", ""))
            if not rid:
                continue
            rem_title = rem.get("title") or "新提醒通知"
            candidates_by_type["reminder"].append(
                {
                    "uid": f"reminder:{rid}",
                    "type": "reminder",
                    "id": rid,
                    "title": f"📬 我的提醒：{rem_title}",
                    "body": (rem.get("body") or "點擊查看提醒內容")[:120],
                    "url": "/?tab=reminders",
                    "tag": f"rem-{rid}",
                    "discovered_at": now_iso,
                }
            )

        newly_added_chronological: list[dict[str, Any]] = []
        new_non_baseline_events: list[dict[str, Any]] = []
        baseline_uids: list[str] = []

        for msg_type in ("announcement", "personal", "reminder"):
            is_type_initialized = msg_type in initialized_types
            for item in candidates_by_type[msg_type]:
                uid = item["uid"]
                if uid not in existing_uids:
                    existing_uids.add(uid)
                    newly_added_chronological.append(item)
                    if is_type_initialized:
                        new_non_baseline_events.append(item)
                    else:
                        baseline_uids.append(uid)
                elif not is_type_initialized:
                    baseline_uids.append(uid)
            initialized_types.add(msg_type)

        # Newest messages stored at the top of messages.json
        updated_messages = (list(reversed(newly_added_chronological)) + existing_messages)[
            :MAX_STORED_MESSAGES
        ]
        self.save_message_store(
            {
                "initialized_types": list(initialized_types),
                "messages": updated_messages,
            }
        )

        # Ensure existing devices have baseline UIDs (or legacy migration) recorded
        subs = self.get_subscriptions()
        subs_modified = False
        all_current_uids = [m["uid"] for m in updated_messages]
        for sub in subs:
            if "pushed_message_uids" not in sub:
                sub["pushed_message_uids"] = list(all_current_uids)[:MAX_DEVICE_PUSHED_UIDS]
                subs_modified = True
            elif baseline_uids:
                merged = list(dict.fromkeys(baseline_uids + sub["pushed_message_uids"]))
                sub["pushed_message_uids"] = merged[:MAX_DEVICE_PUSHED_UIDS]
                subs_modified = True

        if subs_modified:
            self.save_subscriptions(subs)

        # Clean up obsolete seen_state.json if present
        if self.legacy_seen_file.is_file():
            try:
                self.legacy_seen_file.unlink()
            except Exception:
                pass

        return new_non_baseline_events

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
        For each subscribed device, find messages in messages.json that have not
        yet been pushed to that specific device, send Web Push, and record
        successfully pushed message UIDs per device.
        """
        messages = self.get_messages()
        subs = self.get_subscriptions()
        if not subs or not messages:
            return {"sent": 0, "failed": 0, "active_subscribers": len(subs)}

        all_current_uids = [m["uid"] for m in messages if m.get("uid")]
        sent = 0
        failed = 0
        valid_subs: list[dict[str, Any]] = []
        subs_changed = False

        for sub in subs:
            device_id = sub.get("device_id") or _make_device_id(sub.get("endpoint", ""))
            sub["device_id"] = device_id

            # If migrating a legacy subscription before sync_messages ran
            if "pushed_message_uids" not in sub:
                sub["pushed_message_uids"] = list(all_current_uids)[:MAX_DEVICE_PUSHED_UIDS]
                subs_changed = True

            pushed_uids: list[str] = list(sub.get("pushed_message_uids", []))
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
                status = self._send_single_push(sub, msg)
                if status == "sent":
                    sent += 1
                    newly_pushed_for_device.append(msg["uid"])
                    pushed_set.add(msg["uid"])
                elif status == "expired":
                    failed += 1
                    device_expired = True
                    subs_changed = True
                    break
                else:
                    failed += 1

            if not device_expired:
                if newly_pushed_for_device:
                    # Keep most recently pushed UIDs at the front
                    merged = list(
                        dict.fromkeys(list(reversed(newly_pushed_for_device)) + pushed_uids)
                    )
                    sub["pushed_message_uids"] = merged[:MAX_DEVICE_PUSHED_UIDS]
                    subs_changed = True
                valid_subs.append(sub)

        if subs_changed or len(valid_subs) != len(subs):
            self.save_subscriptions(valid_subs)

        return {
            "sent": sent,
            "failed": failed,
            "active_subscribers": len(valid_subs),
        }
