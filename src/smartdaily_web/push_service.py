"""VAPID Web Push Management and Notification State Tracker."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from py_vapid.utils import b64urlencode
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)


class PushService:
    """Manages VAPID keys, browser subscriptions, seen IDs, and Web Push delivery."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        base_dir = Path(data_dir or os.getenv("DATA_DIR", "./data"))
        base_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = base_dir
        self.vapid_file = self.data_dir / "vapid_keys.json"
        self.subs_file = self.data_dir / "subscriptions.json"
        self.seen_file = self.data_dir / "seen_state.json"
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

    def get_subscriptions(self) -> list[dict[str, Any]]:
        if not self.subs_file.is_file():
            return []
        try:
            return json.loads(self.subs_file.read_text(encoding="utf-8"))
        except Exception:
            return []

    def save_subscriptions(self, subs: list[dict[str, Any]]) -> None:
        self.subs_file.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_subscription(self, subscription: dict[str, Any], user_agent: str = "") -> int:
        endpoint = subscription.get("endpoint")
        if not endpoint:
            raise ValueError("Invalid push subscription: missing endpoint")
        subs = [s for s in self.get_subscriptions() if s.get("endpoint") != endpoint]
        subs.append(
            {
                "endpoint": endpoint,
                "keys": subscription.get("keys", {}),
                "user_agent": user_agent,
            }
        )
        self.save_subscriptions(subs)
        return len(subs)

    def remove_subscription(self, endpoint: str) -> int:
        subs = [s for s in self.get_subscriptions() if s.get("endpoint") != endpoint]
        self.save_subscriptions(subs)
        return len(subs)

    def send_push_to_all(
        self,
        title: str,
        body: str,
        url: str = "/",
        tag: str = "smartdaily",
    ) -> dict[str, int]:
        """Send a Web Push notification to all registered subscriptions."""
        subs = self.get_subscriptions()
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "url": url,
                "tag": tag,
            },
            ensure_ascii=False,
        )

        sent = 0
        failed = 0
        valid_subs = []
        for sub in subs:
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
                sent += 1
                valid_subs.append(sub)
            except WebPushException as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code in (404, 410):
                    logger.info("Removing expired push subscription: %s", sub["endpoint"][:50])
                else:
                    logger.warning("WebPush failed (%s): %s", status_code, exc)
                    valid_subs.append(sub)
                failed += 1
            except Exception as exc:
                logger.warning("Unexpected push error: %s", exc)
                valid_subs.append(sub)
                failed += 1

        if len(valid_subs) != len(subs):
            self.save_subscriptions(valid_subs)

        return {"sent": sent, "failed": failed, "active_subscribers": len(valid_subs)}

    def get_seen_state(self) -> dict[str, Any]:
        if not self.seen_file.is_file():
            return {}
        try:
            return json.loads(self.seen_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_seen_state(self, state: dict[str, Any]) -> None:
        self.seen_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def check_and_record_new_items(
        self,
        announcements: list[dict[str, Any]],
        personal_notifications: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """
        Compare current announcements & personal notifications with seen IDs.
        On first run (no seen_state.json), records current IDs without firing notifications.
        Returns a list of new notification events to push.
        """
        state = self.get_seen_state()
        ann_ids = [str(a["id"]) for a in announcements if a.get("id")]
        notif_ids = [str(n["id"]) for n in personal_notifications if n.get("id")]

        is_first_run = not bool(state)
        seen_ann_ids = set(state.get("seen_announcement_ids", []))
        seen_notif_ids = set(state.get("seen_notification_ids", []))

        events: list[dict[str, str]] = []
        if not is_first_run:
            for ann in reversed(announcements):
                aid = str(ann.get("id", ""))
                if aid and aid not in seen_ann_ids:
                    events.append(
                        {
                            "type": "announcement",
                            "id": aid,
                            "title": f"📢 新社區公告：{ann.get('title', '')}",
                            "body": (ann.get("content") or "點擊查看公告詳情與附件")[:120],
                            "url": f"/?tab=announcements&id={aid}",
                            "tag": f"ann-{aid}",
                        }
                    )

            for notif in reversed(personal_notifications):
                nid = str(notif.get("id", ""))
                if nid and nid not in seen_notif_ids:
                    events.append(
                        {
                            "type": "personal",
                            "id": nid,
                            "title": f"🔔 社區個人通知（{notif.get('community', '物管中心')}）",
                            "body": (notif.get("content") or "")[:120],
                            "url": "/?tab=personal",
                            "tag": f"notif-{nid}",
                        }
                    )

        new_state = {
            "seen_announcement_ids": list(dict.fromkeys(ann_ids + list(seen_ann_ids)))[:100],
            "seen_notification_ids": list(dict.fromkeys(notif_ids + list(seen_notif_ids)))[:100],
        }
        self.save_seen_state(new_state)
        return events
