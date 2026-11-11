"""SmartDaily API Client for Announcements, Personal Notifications, and Reminders."""

from __future__ import annotations

import html
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)
# Avoid logging sensitive query parameters (e.g., ?code=DeviceID) in httpx INFO logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SMARTDAILY_API_BASE = "https://api.smartdaily.com.tw/api"
NOTIFICATION_API_BASE = "https://notification.kingnetsmart.com.tw/api"
APP_VERSION = "4.20.7"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)


def clean_html_text(raw_html: str) -> str:
    """Convert HTML breaks/paragraphs to newlines, strip tags, and unescape HTML entities."""
    if not raw_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</h[1-6]\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def filter_community_announcements(announcements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out SmartDaily platform ads where ComId is empty."""
    return [ann for ann in announcements if str(ann.get("ComId") or "").strip() != ""]


class SmartdailyClient:
    """Async client for SmartDaily APIs."""

    def __init__(
        self,
        device_id: str | None = None,
        com_id: str | None = None,
    ) -> None:
        self._explicit_device_id = (device_id or "").strip()
        self._explicit_com_id = (com_id or "").strip()
        self._raw_token: str = ""
        self._token_expires_mono: float = 0.0
        self._com_id: str = self._explicit_com_id
        self._community_name: str = ""

    def resolve_device_id(self) -> str:
        """Resolve DeviceID from init arg or DEVICE_ID environment variable."""
        if self._explicit_device_id:
            return self._explicit_device_id
        env_val = os.getenv("DEVICE_ID", "").strip()
        if env_val:
            return env_val
        raise ValueError("找不到 DeviceID！請設定環境變數 DEVICE_ID。")

    async def refresh_token(self) -> str:
        """Fetch a fresh raw token via APK endpoint POST /v3/Valid/CommunityUserAccessToken."""
        device_id = self.resolve_device_id()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{SMARTDAILY_API_BASE}/v3/Valid/CommunityUserAccessToken",
                json={"DeviceCode": device_id},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "x-app-version": APP_VERSION,
                    "User-Agent": DEFAULT_USER_AGENT,
                },
            )
            resp.raise_for_status()
            data = resp.json().get("Data") or {}
            token = data.get("Token") or data.get("token")
            if not token:
                raise RuntimeError("無法取得智生活 AccessToken，請確認 DEVICE_ID 是否正確")
            self._raw_token = str(token)
            # Token validity is 10 minutes; refresh proactively after 9 minutes
            self._token_expires_mono = time.monotonic() + 540.0
            logger.debug("Refreshed CommunityUserAccessToken (ExpiredAt=%s)", data.get("ExpiredAt"))
            return self._raw_token

    async def ensure_context(self) -> tuple[str, str, str]:
        """Ensure raw_token (unexpired), com_id, and community_name are populated."""
        if not self._raw_token or time.monotonic() >= self._token_expires_mono:
            await self.refresh_token()

        if not self._com_id or not self._community_name:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{SMARTDAILY_API_BASE}/Community/GetUserCommunityList",
                    headers=self._kingnet_headers(),
                )
                if resp.status_code == 401:
                    await self.refresh_token()
                    resp = await client.get(
                        f"{SMARTDAILY_API_BASE}/Community/GetUserCommunityList",
                        headers=self._kingnet_headers(),
                    )
                resp.raise_for_status()
                communities = resp.json().get("Data", [])
                if not communities:
                    raise RuntimeError("此 DeviceID 查無綁定的社區資料")

                target = communities[0]
                if self._com_id:
                    for c in communities:
                        if str(c.get("id")) == str(self._com_id):
                            target = c
                            break
                self._com_id = str(target.get("id", ""))
                self._community_name = str(target.get("community", "我的社區"))

        return self._raw_token, self._com_id, self._community_name

    def _kingnet_headers(self) -> dict[str, str]:
        return {
            "Connection": "keep-alive",
            "KingnetAuth": f"CommunityUser {self._raw_token}",
            "Accept": "application/json, text/plain, */*",
            "x-app-version": APP_VERSION,
            "User-Agent": DEFAULT_USER_AGENT,
        }

    def _notification_box_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json-patch+json",
            "Accept": "application/json, text/plain, */*",
            "authenticationscheme": "SmartlifeDeviceAuthenticationHandler",
            "authorization": f"Bearer {self._raw_token}",
            "x-app-version": APP_VERSION,
            "User-Agent": DEFAULT_USER_AGENT,
        }

    async def get_announcements(self, offset: int = 0, max_count: int = 10) -> dict[str, Any]:
        """Fetch community announcements and filter out ComId == '' platform ads."""
        _, com_id, community_name = await self.ensure_context()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{SMARTDAILY_API_BASE}/v3.24/Announcement/Community/List",
                params={"ComId": com_id, "Offset": offset, "MaxCount": max_count},
                headers=self._kingnet_headers(),
            )
            if resp.status_code == 401:
                await self.refresh_token()
                resp = await client.get(
                    f"{SMARTDAILY_API_BASE}/v3.24/Announcement/Community/List",
                    params={"ComId": com_id, "Offset": offset, "MaxCount": max_count},
                    headers=self._kingnet_headers(),
                )
            resp.raise_for_status()
            raw_list = resp.json().get("Data", {}).get("Announcements", [])

        filtered = filter_community_announcements(raw_list)
        items = []
        for ann in filtered:
            raw_detail = ann.get("Detail") or ann.get("Content") or ""
            items.append(
                {
                    "id": str(ann.get("Id", "")),
                    "com_id": str(ann.get("ComId", "")),
                    "title": html.unescape(str(ann.get("Title", ""))),
                    "start_date": ann.get("Start", ""),
                    "end_date": ann.get("End", ""),
                    "create_date": ann.get("CreateDate", ""),
                    "is_top": ann.get("IsTop", 0) == 2,
                    "type_id": ann.get("Type", 0),
                    "type_name": ann.get("TypeName", "公告"),
                    "already_read": bool(ann.get("AlreadyRead", True)),
                    "content": clean_html_text(raw_detail),
                }
            )

        # Latest non-pinned announcement (by create_date/id) + overall list
        non_top = [i for i in items if not i["is_top"]]
        latest_item = non_top[0] if non_top else (items[0] if items else None)

        return {
            "community_name": community_name,
            "com_id": com_id,
            "total_count": len(items),
            "latest": latest_item,
            "items": items,
        }

    async def get_announcement_detail(self, announcement_id: str) -> dict[str, Any]:
        """Fetch single announcement detail with fresh 15-min signed attachment URLs."""
        _, com_id, _ = await self.ensure_context()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{SMARTDAILY_API_BASE}/v3.2/Announcement/Detail",
                params={"com_id": com_id, "id": announcement_id},
                headers=self._kingnet_headers(),
            )
            if resp.status_code == 401:
                await self.refresh_token()
                resp = await client.get(
                    f"{SMARTDAILY_API_BASE}/v3.2/Announcement/Detail",
                    params={"com_id": com_id, "id": announcement_id},
                    headers=self._kingnet_headers(),
                )
            resp.raise_for_status()
            data = resp.json().get("Data", {})

        raw_content = data.get("Content") or data.get("Detail") or ""
        attached = data.get("AttachedFile") or []
        attachments = []
        for idx, f in enumerate(attached, 1):
            url = f.get("Url", "")
            if not url:
                continue
            file_type = f.get("Type")
            lower_path = url.split("?")[0].lower()
            is_pdf = file_type == 2 or lower_path.endswith(".pdf")
            img_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif")
            is_image = file_type == 1 or lower_path.endswith(img_exts)
            attachments.append(
                {
                    "index": idx,
                    "url": url,
                    "type": "pdf" if is_pdf else ("image" if is_image else "file"),
                    "filename": lower_path.rsplit("/", 1)[-1] or f"附件_{idx}",
                }
            )

        return {
            "id": str(data.get("Id", announcement_id)),
            "title": html.unescape(str(data.get("Title", ""))),
            "type_name": data.get("Type", "公告"),
            "create_date": data.get("CreateDate", ""),
            "content": clean_html_text(raw_content),
            "attachments": attachments,
            "attachment_count": len(attachments),
        }

    async def get_personal_notifications(
        self, offset: int = 0, max_count: int = 10
    ) -> dict[str, Any]:
        """Fetch community personal push notifications (/v3.51/Message/Notification/List)."""
        _, com_id, community_name = await self.ensure_context()
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{SMARTDAILY_API_BASE}/v3.51/Message/Notification/List",
                params={"ComId": com_id, "Offset": offset, "MaxCount": max_count},
                headers=self._kingnet_headers(),
            )
            if resp.status_code == 401:
                await self.refresh_token()
                resp = await client.get(
                    f"{SMARTDAILY_API_BASE}/v3.51/Message/Notification/List",
                    params={"ComId": com_id, "Offset": offset, "MaxCount": max_count},
                    headers=self._kingnet_headers(),
                )
            resp.raise_for_status()
            payload = resp.json().get("Data", {})

        raw_messages = payload.get("Messages", [])
        items = []
        for msg in raw_messages:
            if msg.get("MessageType") != "CommunityPushNotification":
                continue
            files = []
            for f in msg.get("Files") or []:
                if f.get("Url"):
                    files.append(
                        {
                            "url": f.get("Url"),
                            "mime_type": f.get("MimeType", ""),
                        }
                    )
            items.append(
                {
                    "id": str(msg.get("CommunityPushNotificationId") or msg.get("ServiceId") or ""),
                    "community": msg.get("Community", community_name),
                    "content": html.unescape(str(msg.get("Content", ""))).strip(),
                    "push_date": msg.get("PushDate") or msg.get("Sort") or "",
                    "files": files,
                }
            )

        return {
            "community_name": community_name,
            "com_id": com_id,
            "total_count": len(items),
            "latest": items[0] if items else None,
            "items": items,
        }

    async def get_my_reminders(self, before_datetime: str = "") -> dict[str, Any]:
        """Fetch '我的提醒' from NotificationBox/LoadNotices."""
        await self.ensure_context()
        device_id = self.resolve_device_id()
        body = {"DeviceCode": device_id, "BeforeDateTime": before_datetime}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{NOTIFICATION_API_BASE}/NotificationBox/LoadNotices",
                json=body,
                headers=self._notification_box_headers(),
            )
            if resp.status_code == 401:
                await self.refresh_token()
                resp = await client.post(
                    f"{NOTIFICATION_API_BASE}/NotificationBox/LoadNotices",
                    json=body,
                    headers=self._notification_box_headers(),
                )
            resp.raise_for_status()
            raw_notices = resp.json()

        items = []
        for n in raw_notices if isinstance(raw_notices, list) else []:
            msg = n.get("Message") or {}
            msg_data = msg.get("Data") or {}
            feature = msg_data.get("FeatureName", "")
            items.append(
                {
                    "id": str(n.get("Id", "")),
                    "pushing_time": n.get("PushingTime", ""),
                    "has_read": bool(n.get("HasRead", True)),
                    "title": msg.get("Title", ""),
                    "body": msg.get("Body", ""),
                    "feature_name": feature,
                    "target_id": str(msg_data.get("Id", "")),
                    "files": n.get("Files") or [],
                }
            )

        return {
            "total_count": len(items),
            "latest": items[0] if items else None,
            "items": items,
        }
