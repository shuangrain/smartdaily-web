"""Unit tests for smartdaily-web."""

from pathlib import Path

from smartdaily_web.push_service import PushService
from smartdaily_web.smartdaily_client import (
    clean_html_text,
    filter_community_announcements,
)


def test_filter_community_announcements():
    raw = [
        {"Id": "1", "ComId": "", "Title": "智生活平台冷氣清洗廣告"},
        {"Id": "2", "ComId": "25122307", "Title": "住戶專有層梯廳後續修繕說明"},
        {"Id": "3", "ComId": None, "Title": "無社區ID廣告"},
    ]
    filtered = filter_community_announcements(raw)
    assert len(filtered) == 1
    assert filtered[0]["Id"] == "2"


def test_clean_html_text():
    raw = "<p>親愛的住戶們：<br />&bull; 第一點&nbsp;<br>&ndash; 第二點</p>"
    cleaned = clean_html_text(raw)
    assert "• 第一點" in cleaned
    assert "– 第二點" in cleaned
    assert "<p>" not in cleaned


def test_push_service_vapid_and_diffing(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    assert len(svc.public_key) > 40
    assert len(svc.private_key) > 20

    # First run: initializes baseline without triggering push events
    events1 = svc.check_and_record_new_items(
        announcements=[{"id": "100", "title": "舊公告", "content": "內容"}],
        personal_notifications=[{"id": "200", "community": "測試社區", "content": "舊通知"}],
    )
    assert events1 == []

    # Second run: new announcement & new personal notification arrive
    events2 = svc.check_and_record_new_items(
        announcements=[
            {"id": "101", "title": "新公告", "content": "新內容"},
            {"id": "100", "title": "舊公告", "content": "內容"},
        ],
        personal_notifications=[
            {"id": "201", "community": "測試社區", "content": "新推播通知"},
            {"id": "200", "community": "測試社區", "content": "舊通知"},
        ],
    )
    assert len(events2) == 2
    assert any(e["type"] == "announcement" and e["id"] == "101" for e in events2)
    assert any(e["type"] == "personal" and e["id"] == "201" for e in events2)


def test_push_subscribe_and_unsubscribe(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    sub = {
        "endpoint": "https://web.push.apple.com/test-endpoint",
        "keys": {"p256dh": "k", "auth": "a"},
    }
    assert svc.add_subscription(sub, user_agent="iPhone") == 1
    assert len(svc.get_subscriptions()) == 1
    assert svc.remove_subscription("https://web.push.apple.com/test-endpoint") == 0
    assert len(svc.get_subscriptions()) == 0
