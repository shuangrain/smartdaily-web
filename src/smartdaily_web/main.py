"""FastAPI PWA Server for SmartDaily Community Announcements & Personal Notifications."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("smartdaily_web")
logger.setLevel(LOG_LEVEL)

STATIC_DIR = Path(__file__).parent / "static"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))

client = SmartdailyClient()
push_service = PushService()

poll_status: dict[str, Any] = {
    "last_poll_at": None,
    "last_error": None,
    "poll_interval_seconds": POLL_INTERVAL_SECONDS,
}


async def poll_once() -> dict[str, Any]:
    """Fetch latest announcements and personal notifications and push any new items."""
    try:
        ann_data, personal_data = await asyncio.gather(
            client.get_announcements(offset=0, max_count=10),
            client.get_personal_notifications(offset=0, max_count=10),
        )
        ann_items = ann_data.get("items", [])
        personal_items = personal_data.get("items", [])
        logger.debug(
            "Polled %d announcements, %d personal notifications",
            len(ann_items),
            len(personal_items),
        )
        events = push_service.check_and_record_new_items(
            announcements=ann_items,
            personal_notifications=personal_items,
        )
        pushed_count = 0
        for ev in events:
            logger.info("New %s detected (%s): %s", ev["type"], ev["id"], ev["title"])
            res = push_service.send_push_to_all(
                title=ev["title"],
                body=ev["body"],
                url=ev["url"],
                tag=ev["tag"],
            )
            pushed_count += res.get("sent", 0)

        poll_status["last_poll_at"] = datetime.now(timezone.utc).isoformat()
        poll_status["last_error"] = None
        return {
            "new_events_count": len(events),
            "pushed_count": pushed_count,
            "events": events,
        }
    except Exception as exc:
        logger.warning("Polling check failed: %s", exc)
        poll_status["last_error"] = str(exc)
        raise


async def background_poll_loop() -> None:
    """Background task to periodically poll SmartDaily API."""
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
    try:
        _, com_id, community_name = await client.ensure_context()
        configured = True
        error = None
    except Exception as exc:
        com_id = ""
        community_name = "尚未綁定社區"
        configured = False
        error = str(exc)

    return {
        "configured": configured,
        "community_name": community_name,
        "com_id": com_id,
        "error": error,
        "subscribers_count": len(push_service.get_subscriptions()),
        "poll": poll_status,
    }


@app.get("/api/announcements")
async def list_announcements(offset: int = 0, max_count: int = 10) -> dict[str, Any]:
    try:
        return await client.get_announcements(offset=offset, max_count=max_count)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/announcements/{announcement_id}")
async def get_announcement_detail(announcement_id: str) -> dict[str, Any]:
    try:
        return await client.get_announcement_detail(announcement_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/notifications/personal")
async def list_personal_notifications(offset: int = 0, max_count: int = 10) -> dict[str, Any]:
    try:
        return await client.get_personal_notifications(offset=offset, max_count=max_count)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/notifications/reminders")
async def list_reminders(before_datetime: str = "") -> dict[str, Any]:
    try:
        return await client.get_my_reminders(before_datetime=before_datetime)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
    title = payload.title if payload and payload.title else None
    body = payload.body if payload and payload.body else None

    if not title or not body:
        try:
            ann = await client.get_announcements(offset=0, max_count=5)
            personal = await client.get_personal_notifications(offset=0, max_count=5)
            latest_ann = ann.get("latest")
            latest_p = personal.get("latest")
            title = title or f"🔔 [{ann.get('community_name', '社區')}] 推播測試成功"
            parts = []
            if latest_ann:
                parts.append(f"📢 最新公告：{latest_ann['title']}")
            if latest_p:
                parts.append(f"💬 最新通知：{latest_p['content'][:60]}...")
            default_body = "\n".join(parts) if parts else "您的 iOS / 瀏覽器 Web Push 已正常運作！"
            body = body or default_body
        except Exception:
            title = title or "🔔 智生活社區通知測試"
            body = body or "您的 Web Push 推播通知已正常運作！"

    result = push_service.send_push_to_all(
        title=title,
        body=body,
        url="/",
        tag="smartdaily-test",
    )
    return {"ok": True, **result, "title": title, "body": body}


@app.post("/api/poll-now")
async def trigger_poll_now() -> dict[str, Any]:
    try:
        return await poll_once()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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
