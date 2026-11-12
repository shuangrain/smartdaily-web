"""FastAPI PWA Server for SmartDaily Community Announcements & Personal Notifications."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .push_service import PushService
from .smartdaily_client import SmartdailyClient

LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").strip().upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)
LOG_FORMAT = "%(asctime)s [%(levelname)s] [trace_id=%(trace_id)s] %(name)s: %(message)s"

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

_orig_record_factory = logging.getLogRecordFactory()


def _trace_record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _orig_record_factory(*args, **kwargs)
    if not hasattr(record, "trace_id"):
        record.trace_id = trace_id_var.get("-")
    return record


logging.setLogRecordFactory(_trace_record_factory)
logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(logging.Formatter(LOG_FORMAT))

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("smartdaily_web")
logger.setLevel(LOG_LEVEL)

STATIC_DIR = Path(__file__).parent / "static"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))
FULL_SYNC_INTERVAL_SECONDS = int(os.getenv("FULL_SYNC_INTERVAL_SECONDS", "86400"))

client = SmartdailyClient()
push_service = PushService()

poll_status: dict[str, Any] = {
    "last_poll_at": None,
    "last_trace_id": None,
    "last_error": None,
    "is_running": False,
    "poll_interval_seconds": POLL_INTERVAL_SECONDS,
}
poll_lock = asyncio.Lock()
manual_poll_task: asyncio.Task[None] | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_full_sync_due(last_full_sync_at: str | None, now: datetime) -> bool:
    """Return whether a complete history sync is due or its saved time is invalid."""
    if not last_full_sync_at:
        return True

    try:
        last_sync = datetime.fromisoformat(str(last_full_sync_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True

    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    return now >= last_sync + timedelta(seconds=FULL_SYNC_INTERVAL_SECONDS)


async def ensure_announcement_attachments_cached(announcement_id: str) -> dict[str, Any] | None:
    """Fetch announcement detail and download/backup attachments to local storage."""
    try:
        detail = await client.get_announcement_detail(announcement_id)
        if detail.get("is_deleted") or (not detail.get("title") and not detail.get("content")):
            push_service.mark_announcement_deleted(announcement_id)
            return None
        raw_atts = detail.get("attachments", [])
        downloaded = await client.download_attachment_files(raw_atts)
        saved_atts = push_service.save_announcement_detail_and_attachments(
            announcement_id=announcement_id,
            detail=detail,
            downloaded_files=downloaded,
        )
        detail["attachments"] = saved_atts
        detail["attachment_count"] = len(saved_atts)
        logger.info(
            "Cached %d/%d attachment(s) for announcement %s (%s)",
            len(downloaded),
            len(raw_atts),
            announcement_id,
            detail.get("title", ""),
        )
        return detail
    except Exception as exc:
        logger.warning("Could not cache attachments for announcement %s: %s", announcement_id, exc)
        return None


async def fetch_all_announcements(
    page_size: int = 10,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch every announcement page, retaining the first page's community metadata."""
    first_data: dict[str, Any] = {}
    items_by_id: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        data = await client.get_announcements(offset=offset, max_count=page_size)
        if not first_data:
            first_data = data
        for item in data.get("items", []):
            item_id = str(item.get("id", ""))
            if item_id:
                items_by_id[item_id] = item
        if not data.get("has_more"):
            break
        offset += page_size
    return first_data, list(items_by_id.values())


async def fetch_all_reminders() -> list[dict[str, Any]]:
    """Fetch reminder pages until SmartDaily returns the final page."""
    items_by_id: dict[str, dict[str, Any]] = {}
    before_datetime = ""
    while True:
        data = await client.get_my_reminders(before_datetime=before_datetime)
        items = data.get("items", [])
        for item in items:
            item_id = str(item.get("id", ""))
            if item_id:
                items_by_id[item_id] = item
        if not data.get("has_more") or not items:
            break
        next_before_datetime = str(data.get("next_before_datetime", ""))
        if not next_before_datetime or next_before_datetime == before_datetime:
            logger.warning("Reminder pagination stopped because its cursor did not advance")
            break
        before_datetime = next_before_datetime
    return list(items_by_id.values())


async def poll_once() -> dict[str, Any]:
    """Incrementally sync recent messages and periodically refresh the full history."""
    if poll_lock.locked():
        logger.info("Poll skipped because another poll is already running")
        return {"skipped": True, "reason": "already_running"}

    await poll_lock.acquire()
    trace_id = str(uuid.uuid4())
    token = trace_id_var.set(trace_id)
    poll_status["is_running"] = True
    try:
        poll_status["last_trace_id"] = trace_id
        poll_started_at = _utc_now()
        metadata = push_service.get_store_metadata()
        full_sync = _is_full_sync_due(metadata.get("last_full_sync_at"), poll_started_at)
        logger.info(
            "Poll started (full_sync=%s, full_sync_interval=%ds)",
            full_sync,
            FULL_SYNC_INTERVAL_SECONDS,
        )
        try:
            if full_sync:
                ann_result, rem_items = await asyncio.gather(
                    fetch_all_announcements(),
                    fetch_all_reminders(),
                )
                ann_data, ann_items = ann_result
            else:
                ann_data, reminder_data = await asyncio.gather(
                    client.get_announcements(offset=0, max_count=10),
                    client.get_my_reminders(),
                )
                ann_items = ann_data.get("items", [])
                rem_items = reminder_data.get("items", [])

            logger.info(
                "Polled %d announcements and %d reminders",
                len(ann_items),
                len(rem_items),
            )
            events = push_service.sync_messages(
                announcements=ann_items,
                reminders=rem_items,
                full_sync=full_sync,
            )
            sync_completed_at = _utc_now()
            metadata_updates = {
                "community_name": ann_data.get("community_name", ""),
                "com_id": ann_data.get("com_id", ""),
                "last_synced_at": sync_completed_at.isoformat(),
            }
            if full_sync:
                metadata_updates["last_full_sync_at"] = sync_completed_at.isoformat()
            push_service.update_store_metadata(**metadata_updates)
            for ev in events:
                logger.info("New %s recorded (%s): %s", ev["type"], ev["id"], ev["title"])

            # Cache full announcement details and attachments in the background so
            # frontend detail views can stay entirely local.
            for aid in push_service.get_uncached_announcement_ids():
                await ensure_announcement_attachments_cached(aid)

            push_res = await asyncio.to_thread(push_service.push_unpushed_messages)
            pushed_count = push_res.get("sent", 0)
            failed_count = push_res.get("failed", 0)
            active_subs = push_res.get("active_subscribers", 0)

            poll_status["last_poll_at"] = _utc_now().isoformat()
            poll_status["last_error"] = None
            logger.info(
                "Poll completed: announcements=%d, reminders=%d, new_events=%d, pushed=%d, "
                "failed=%d, active_subscribers=%d",
                len(ann_items),
                len(rem_items),
                len(events),
                pushed_count,
                failed_count,
                active_subs,
            )
            return {
                "trace_id": trace_id,
                "new_events_count": len(events),
                "pushed_count": pushed_count,
                "events": events,
            }
        except Exception as exc:
            logger.warning("Polling check failed: %s", exc)
            poll_status["last_error"] = str(exc)
            raise
    finally:
        poll_status["is_running"] = False
        poll_lock.release()
        trace_id_var.reset(token)


async def _run_manual_poll() -> None:
    try:
        await poll_once()
    except Exception:
        # poll_once records the error and log entry; keep the detached task handled.
        pass


def schedule_manual_poll() -> dict[str, Any]:
    """Schedule one poll in the background, skipping it if a poll is active."""
    global manual_poll_task
    if poll_lock.locked() or (manual_poll_task and not manual_poll_task.done()):
        return {"started": False, "status": "already_running"}
    manual_poll_task = asyncio.create_task(_run_manual_poll())
    return {"started": True, "status": "scheduled"}


async def background_poll_loop() -> None:
    """Background task to periodically poll SmartDaily API."""
    logger.info("Background poll loop started (interval=%ds)", POLL_INTERVAL_SECONDS)
    # Initial delay briefly after startup
    await asyncio.sleep(2)
    while True:
        try:
            await poll_once()
        except Exception:
            pass
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    push_service.migrate()
    task = None
    if os.getenv("DISABLE_BACKGROUND_POLL", "") != "1":
        task = asyncio.create_task(background_poll_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()


app = FastAPI(
    title="智生活社區公告與個人通知 PWA",
    version="0.1.0",
    lifespan=lifespan,
)


class PushSubscriptionPayload(BaseModel):
    endpoint: str
    keys: dict[str, str]


class UnsubscribePayload(BaseModel):
    endpoint: str


class TestPushPayload(BaseModel):
    title: str | None = None
    body: str | None = None


@app.get("/api/status")
async def get_status() -> dict[str, Any]:
    metadata = push_service.get_store_metadata()
    com_id = str(metadata.get("com_id", ""))
    community_name = str(metadata.get("community_name") or "尚未同步社區資料")

    return {
        "configured": bool(com_id),
        "community_name": community_name,
        "com_id": com_id,
        "error": poll_status["last_error"],
        "subscribers_count": len(push_service.get_subscriptions()),
        "stored_messages_count": len(push_service.get_messages()),
        "poll": {
            **poll_status,
            "last_poll_at": poll_status["last_poll_at"] or metadata.get("last_synced_at"),
        },
    }


@app.get("/api/announcements")
async def list_announcements(offset: int = 0, max_count: int = 10) -> dict[str, Any]:
    items, total = push_service.get_message_page("announcement", offset, max_count)
    metadata = push_service.get_store_metadata()
    return {
        "community_name": metadata.get("community_name", ""),
        "com_id": metadata.get("com_id", ""),
        "offset": max(0, offset),
        "max_count": max(1, min(max_count, 100)),
        "has_more": max(0, offset) + len(items) < total,
        "total_count": total,
        "latest": items[0] if items else None,
        "items": items,
    }


@app.get("/api/announcements/{announcement_id}")
async def get_announcement_detail(announcement_id: str) -> dict[str, Any]:
    stored = push_service.get_stored_announcement_detail(announcement_id)
    if not stored:
        stored = push_service.get_announcement_reminder_fallback(announcement_id)
    if not stored:
        raise HTTPException(status_code=404, detail="本機尚未收錄此公告")
    return stored


@app.get("/api/attachments/{announcement_id}/{filename}")
async def serve_cached_attachment(announcement_id: str, filename: str) -> FileResponse:
    file_path = push_service.get_attachment_file_path(announcement_id, filename)
    if not file_path:
        raise HTTPException(status_code=404, detail="找不到本地備份附件")
    return FileResponse(file_path)


@app.get("/api/notifications/reminders")
async def list_reminders(offset: int = 0, max_count: int = 10) -> dict[str, Any]:
    items, total = push_service.get_message_page("reminder", offset, max_count)
    has_more = max(0, offset) + len(items) < total
    return {
        "offset": max(0, offset),
        "max_count": max(1, min(max_count, 100)),
        "has_more": has_more,
        "total_count": total,
        "latest": items[0] if items else None,
        "next_before_datetime": items[-1].get("pushing_time", "") if has_more and items else "",
        "items": items,
    }


@app.get("/api/push/vapid-public-key")
async def get_vapid_public_key() -> dict[str, str]:
    return {"public_key": push_service.public_key}


@app.post("/api/push/subscribe")
async def subscribe_push(payload: PushSubscriptionPayload, request: Request) -> dict[str, Any]:
    ua = request.headers.get("user-agent", "")
    count = push_service.add_subscription(payload.model_dump(), user_agent=ua)
    return {"ok": True, "subscribers_count": count}


@app.post("/api/push/unsubscribe")
async def unsubscribe_push(payload: UnsubscribePayload) -> dict[str, Any]:
    count = push_service.remove_subscription(payload.endpoint)
    return {"ok": True, "subscribers_count": count}


@app.post("/api/push/test")
async def send_test_push(payload: TestPushPayload | None = None) -> dict[str, Any]:
    trace_id = str(uuid.uuid4())
    token = trace_id_var.set(trace_id)
    try:
        title = payload.title if payload and payload.title else None
        body = payload.body if payload and payload.body else None

        if not title or not body:
            metadata = push_service.get_store_metadata()
            latest_ann_items, _ = push_service.get_message_page("announcement", 0, 1)
            latest_rem_items, _ = push_service.get_message_page("reminder", 0, 1)
            latest_ann = latest_ann_items[0] if latest_ann_items else None
            latest_rem = latest_rem_items[0] if latest_rem_items else None
            community_name = metadata.get("community_name") or "社區"
            title = title or f"🔔 [推播測試] {community_name} 通知連線正常"
            parts = []
            if latest_ann:
                parts.append(f"📢 最新公告：{latest_ann.get('title', '')}")
            if latest_rem:
                parts.append(f"📬 最新提醒：{str(latest_rem.get('body', ''))[:60]}...")
            body = body or (
                "\n".join(parts) if parts else "您的 iOS / 瀏覽器 Web Push 已正常運作！"
            )

        result = await asyncio.to_thread(
            push_service.send_push_to_all,
            title=title,
            body=body,
            url="/",
            tag="smartdaily-test",
        )
        return {"ok": True, "trace_id": trace_id, **result, "title": title, "body": body}
    finally:
        trace_id_var.reset(token)


@app.post("/api/poll-now")
async def trigger_poll_now() -> dict[str, Any]:
    return schedule_manual_poll()


@app.get("/manifest.json")
async def serve_manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/sw.js")
async def serve_sw() -> FileResponse:
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


@app.get("/")
async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "smartdaily_web.main:app",
        host=host,
        port=port,
        log_level=LOG_LEVEL_STR.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
