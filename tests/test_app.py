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


def test_push_service_vapid_and_per_device_push_tracking(tmp_path: Path, monkeypatch):
    svc = PushService(data_dir=tmp_path)
    assert len(svc.public_key) > 40
    assert len(svc.private_key) > 20

    # Subscribe Device A before first sync
    sub_a = {
        "endpoint": "https://web.push.apple.com/device-a",
        "keys": {"p256dh": "ka", "auth": "aa"},
    }
    svc.add_subscription(sub_a, user_agent="iPhone-A")

    # First run: initializes central message store & marks baseline on Device A without pushing
    events1 = svc.sync_messages(
        announcements=[{"id": "100", "title": "舊公告", "content": "內容"}],
        personal_notifications=[{"id": "200", "community": "測試社區", "content": "舊通知"}],
        reminders=[{"id": "300", "title": "舊提醒", "body": "舊包裹"}],
    )
    assert events1 == []
    assert len(svc.get_messages()) == 3

    subs_after_init = svc.get_subscriptions()
    assert len(subs_after_init) == 1
    assert set(subs_after_init[0]["pushed_message_uids"]) == {
        "announcement:100",
        "personal:200",
        "reminder:300",
    }

    # Subscribe Device B after baseline is already in messages.json
    sub_b = {
        "endpoint": "https://web.push.apple.com/device-b",
        "keys": {"p256dh": "kb", "auth": "ab"},
    }
    svc.add_subscription(sub_b, user_agent="iPad-B")
    subs_with_b = svc.get_subscriptions()
    dev_b = next(s for s in subs_with_b if s["endpoint"] == sub_b["endpoint"])
    assert set(dev_b["pushed_message_uids"]) == {
        "announcement:100",
        "personal:200",
        "reminder:300",
    }

    # Second run: new announcement, new personal notification, & new reminder arrive
    events2 = svc.sync_messages(
        announcements=[
            {"id": "101", "title": "新公告", "content": "新內容"},
            {"id": "100", "title": "舊公告", "content": "內容"},
        ],
        personal_notifications=[
            {"id": "201", "community": "測試社區", "content": "新推播通知"},
            {"id": "200", "community": "測試社區", "content": "舊通知"},
        ],
        reminders=[
            {"id": "301", "title": "包裹到件通知", "body": "您有一件包裹已送達管理室"},
            {"id": "300", "title": "舊提醒", "body": "舊包裹"},
        ],
    )
    assert len(events2) == 3
    assert len(svc.get_messages()) == 6

    # Simulate Device A succeeding all 3 pushes, while Device B temporarily fails on reminder:301
    delivery_log: list[tuple[str, str]] = []

    def fake_send_single_push(sub, payload_dict):
        uid = f"{payload_dict['type']}:{payload_dict['id']}"
        if sub["endpoint"].endswith("device-b") and uid == "reminder:301":
            return "failed"
        delivery_log.append((sub["endpoint"], uid))
        return "sent"

    monkeypatch.setattr(svc, "_send_single_push", fake_send_single_push)
    res1 = svc.push_unpushed_messages()
    assert res1["sent"] == 5
    assert res1["failed"] == 1

    subs_state = svc.get_subscriptions()
    dev_a_state = next(s for s in subs_state if s["endpoint"] == sub_a["endpoint"])
    dev_b_state = next(s for s in subs_state if s["endpoint"] == sub_b["endpoint"])
    assert "reminder:301" in dev_a_state["pushed_message_uids"]
    assert "reminder:301" not in dev_b_state["pushed_message_uids"]

    # Next push cycle: Device A gets 0 pushes, Device B retries ONLY reminder:301 and succeeds
    delivery_log.clear()
    monkeypatch.setattr(
        svc,
        "_send_single_push",
        lambda sub, msg: delivery_log.append((sub["endpoint"], msg["uid"])) or "sent",
    )
    res2 = svc.push_unpushed_messages()
    assert res2["sent"] == 1
    assert res2["failed"] == 0
    assert delivery_log == [(sub_b["endpoint"], "reminder:301")]


def test_push_subscribe_and_unsubscribe(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    sub1 = {
        "endpoint": "https://web.push.apple.com/test-endpoint-1",
        "keys": {"p256dh": "k1", "auth": "a1"},
    }
    sub2 = {
        "endpoint": "https://web.push.apple.com/test-endpoint-2",
        "keys": {"p256dh": "k2", "auth": "a2"},
    }
    assert svc.add_subscription(sub1, user_agent="iPhone") == 1
    assert svc.add_subscription(sub2, user_agent="iPad") == 2
    assert len(list((tmp_path / "devices").glob("*.json"))) == 2
    assert svc.remove_subscription("https://web.push.apple.com/test-endpoint-1") == 1
    assert len(list((tmp_path / "devices").glob("*.json"))) == 1
    assert svc.remove_subscription("https://web.push.apple.com/test-endpoint-2") == 0
    assert len(list((tmp_path / "devices").glob("*.json"))) == 0


def test_recalled_message_is_deleted_and_merged_for_display(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    svc.sync_messages(
        announcements=[
            {"id": "101", "title": "誤發的公告", "content": "這則之後會被回收"},
            {"id": "100", "title": "正常公告", "content": "正常保留"},
        ],
        personal_notifications=[],
        reminders=[],
    )
    msgs_initial = svc.get_messages()
    assert len(msgs_initial) == 2
    assert all(m["is_deleted"] is False for m in msgs_initial)

    # Now announcement 101 is recalled on SmartDaily (only 100 is returned by live API)
    merged = svc.merge_with_deleted_items(
        "announcement",
        [{"id": "100", "title": "正常公告", "content": "正常保留"}],
        is_first_page=True,
    )
    assert len(merged) == 2
    recalled = next(i for i in merged if i["id"] == "101")
    active = next(i for i in merged if i["id"] == "100")
    assert recalled["is_deleted"] is True
    assert recalled["title"] == "誤發的公告"
    assert active["is_deleted"] is False

    detail = svc.get_stored_announcement_detail("101")
    assert detail is not None
    assert detail["is_deleted"] is True
    assert detail["content"] == "這則之後會被回收"

    # Verify local attachment backup & retrieval even when announcement is deleted
    saved_atts = svc.save_announcement_detail_and_attachments(
        announcement_id="101",
        detail={
            "id": "101",
            "title": "誤發的公告",
            "content": "完整公告詳情與附件",
            "attachments": [
                {
                    "index": 1,
                    "url": "https://storage.googleapis.com/signed-url",
                    "type": "pdf",
                    "filename": "notice.pdf",
                }
            ],
        },
        downloaded_files={1: ("1_notice.pdf", b"%PDF-1.4 dummy")},
    )
    assert len(saved_atts) == 1
    assert saved_atts[0]["url"] == "/api/attachments/101/1_notice.pdf"
    local_path = svc.get_attachment_file_path("101", "1_notice.pdf")
    assert local_path is not None and local_path.read_bytes() == b"%PDF-1.4 dummy"

    detail_with_att = svc.get_stored_announcement_detail("101")
    assert detail_with_att is not None
    assert detail_with_att["attachment_count"] == 1
    assert detail_with_att["attachments"][0]["url"] == "/api/attachments/101/1_notice.pdf"
