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
        reminders=[{"id": "300", "feature_name": "postal", "title": "舊提醒", "body": "舊包裹"}],
    )
    assert events1 == []
    assert len(svc.get_messages()) == 2

    subs_after_init = svc.get_subscriptions()
    assert len(subs_after_init) == 1
    assert set(subs_after_init[0]["pushed_message_uids"]) == {
        "announcement:100",
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
        "reminder:300",
    }

    # Second run: new announcement & 2 new reminders arrive
    # Announcements are stored for display/backup; reminders trigger Web Push with [類型] prefix
    events2 = svc.sync_messages(
        announcements=[
            {"id": "101", "title": "新公告", "content": "新內容"},
            {"id": "100", "title": "舊公告", "content": "內容"},
        ],
        reminders=[
            {
                "id": "302",
                "feature_name": "Ann",
                "target_id": "101",
                "title": "新公告通知",
                "body": "新公告",
            },
            {
                "id": "301",
                "feature_name": "postal",
                "title": "包裹到件通知",
                "body": "您有一件包裹已送達管理室",
            },
            {"id": "300", "feature_name": "postal", "title": "舊提醒", "body": "舊包裹"},
        ],
    )
    assert len(events2) == 2
    assert events2[0]["title"] == "[📦 郵務包裹] 包裹到件通知"
    assert events2[0]["url"] == "/?tab=reminders"
    assert events2[1]["title"] == "[📢 社區公告] 新公告通知"
    assert events2[1]["url"] == "/?tab=announcements&id=101"
    assert len(svc.get_messages()) == 5

    # Simulate Device A succeeding both pushes, while Device B fails on reminder:301
    delivery_log: list[tuple[str, str]] = []

    def fake_send_single_push(sub, payload_dict):
        uid = f"{payload_dict['type']}:{payload_dict['id']}"
        if sub["endpoint"].endswith("device-b") and uid == "reminder:301":
            return "failed"
        delivery_log.append((sub["endpoint"], uid))
        return "sent"

    monkeypatch.setattr(svc, "_send_single_push", fake_send_single_push)
    res1 = svc.push_unpushed_messages()
    assert res1["sent"] == 3
    assert res1["failed"] == 1

    subs_state = svc.get_subscriptions()
    dev_a_state = next(s for s in subs_state if s["endpoint"] == sub_a["endpoint"])
    dev_b_state = next(s for s in subs_state if s["endpoint"] == sub_b["endpoint"])
    assert "reminder:301" in dev_a_state["pushed_message_uids"]
    assert "reminder:302" in dev_a_state["pushed_message_uids"]
    assert "reminder:301" not in dev_b_state["pushed_message_uids"]
    assert "reminder:302" in dev_b_state["pushed_message_uids"]

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


def test_poll_once_trace_id_in_logs(tmp_path: Path, monkeypatch, caplog):
    import asyncio
    import importlib
    import logging

    main_mod = importlib.import_module("smartdaily_web.main")

    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)

    async def fake_get_announcements(offset: int = 0, max_count: int = 10):
        return {"items": [{"id": "201", "title": "測試公告", "content": "內容"}]}

    async def fake_get_reminders(before_datetime: str = ""):
        return {
            "items": [
                {
                    "id": "r-1",
                    "feature_name": "postal",
                    "title": "包裹到件通知",
                    "body": "包裹",
                }
            ]
        }

    async def fake_cache_atts(aid: str):
        return None

    monkeypatch.setattr(main_mod.client, "get_announcements", fake_get_announcements)
    monkeypatch.setattr(main_mod.client, "get_my_reminders", fake_get_reminders)
    monkeypatch.setattr(main_mod, "ensure_announcement_attachments_cached", fake_cache_atts)

    with caplog.at_level(logging.INFO):
        res = asyncio.run(main_mod.poll_once())

    trace_id = res["trace_id"]
    assert len(trace_id) == 36
    poll_records = [r for r in caplog.records if getattr(r, "trace_id", None) == trace_id]
    assert len(poll_records) >= 3
    messages = [r.getMessage() for r in poll_records]
    assert any("Poll started" in m for m in messages)
    assert any("Polled 1 announcements, 1 reminders" in m for m in messages)
    assert any("Poll completed:" in m for m in messages)


def test_legacy_subscription_migration_and_expired_device_cleanup(tmp_path: Path, monkeypatch):
    import json

    legacy_file = tmp_path / "subscriptions.json"
    legacy_file.write_text(
        json.dumps(
            [
                {
                    "endpoint": "https://web.push.apple.com/legacy-1",
                    "keys": {"p256dh": "k1", "auth": "a1"},
                },
                {
                    "endpoint": "https://web.push.apple.com/legacy-2",
                    "keys": {"p256dh": "k2", "auth": "a2"},
                },
            ]
        ),
        encoding="utf-8",
    )

    svc = PushService(data_dir=tmp_path)
    assert not legacy_file.exists()
    subs = svc.get_subscriptions()
    assert len(subs) == 2

    # Initialize baseline then add 1 new reminder
    svc.sync_messages(reminders=[])
    svc.sync_messages(
        reminders=[
            {
                "id": "rem-exp-1",
                "feature_name": "Col",
                "title": "寄放物領取通知",
                "body": "您有寄放物",
            }
        ]
    )

    # Simulate legacy-1 returning 'expired' (410 Gone) and legacy-2 succeeding
    def fake_send(sub, msg):
        if sub["endpoint"].endswith("legacy-1"):
            return "expired"
        return "sent"

    monkeypatch.setattr(svc, "_send_single_push", fake_send)
    res = svc.push_unpushed_messages()
    assert res["sent"] == 1
    assert res["failed"] == 1
    assert res["active_subscribers"] == 1
    remaining = svc.get_subscriptions()
    assert len(remaining) == 1
    assert remaining[0]["endpoint"].endswith("legacy-2")


def test_reminder_iso_timestamp_pagination_cutoff_and_recall_detection(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    # Initial 10 reminders on page 1 (t01 oldest .. t10 newest)
    initial_reminders = [
        {
            "id": f"uuid-{i:02d}",
            "pushing_time": f"2026-11-10T10:{i:02d}:00.000000Z",
            "feature_name": "CommunityPushNotification" if i == 10 else "postal",
            "title": f"提醒 {i}",
            "body": f"內容 {i}",
            "files": [{"url": "https://example.com/img10.jpg", "mime_type": "image/jpeg"}]
            if i == 10
            else [],
        }
        for i in range(10, 0, -1)
    ]
    svc.sync_messages(reminders=initial_reminders)

    # Next poll:
    # - uuid-11 arrives at 10:11
    # - uuid-09 (10:09) was recalled/deleted!
    # - uuid-01 (10:01) was pushed to page 2 (cutoff is uuid-02 at 10:02)
    next_page1 = [
        {
            "id": "uuid-11",
            "pushing_time": "2026-11-10T10:11:00.000000Z",
            "feature_name": "postal",
            "title": "提醒 11",
            "body": "內容 11",
            "files": [],
        },
        {
            "id": "uuid-10",
            "pushing_time": "2026-11-10T10:10:00.000000Z",
            "feature_name": "CommunityPushNotification",
            "title": "提醒 10",
            "body": "內容 10",
            "files": [],  # Even if empty on a later sync, previously enriched files are preserved
        },
    ] + [
        {
            "id": f"uuid-{i:02d}",
            "pushing_time": f"2026-11-10T10:{i:02d}:00.000000Z",
            "feature_name": "postal",
            "title": f"提醒 {i}",
            "body": f"內容 {i}",
            "files": [],
        }
        for i in range(8, 0, -1)
    ]
    # Replace last item (uuid-01) with uuid-08b at 10:08:30 so cutoff is 10:02
    next_page1[-1] = {
        "id": "uuid-08b",
        "pushing_time": "2026-11-10T10:08:30.000000Z",
        "feature_name": "postal",
        "title": "提醒 8b",
        "body": "內容 8b",
        "files": [],
    }
    assert len(next_page1) == 10

    merged = svc.merge_with_deleted_items("reminder", next_page1, is_first_page=True)
    by_id = {m["id"]: m for m in svc.get_messages()}
    # uuid-09 (10:09 >= cutoff 10:02) was recalled -> is_deleted=True
    assert by_id["uuid-09"]["is_deleted"] is True
    # uuid-01 (10:01 < cutoff 10:02) was merely pushed to page 2 -> is_deleted=False!
    assert by_id["uuid-01"]["is_deleted"] is False
    # uuid-10 preserved its enriched image files
    assert len(by_id["uuid-10"]["item"]["files"]) == 1
    # Merged first-page result includes recalled uuid-09 sorted in chronological order
    merged_ids = [m["id"] for m in merged]
    assert "uuid-09" in merged_ids
    assert "uuid-01" not in merged_ids


def test_smartdaily_client_pagination_404_detail_and_reminder_files(monkeypatch):
    import asyncio

    import httpx

    from smartdaily_web.smartdaily_client import SmartdailyClient

    client = SmartdailyClient(device_id="dummy-device", com_id="25122307")
    client._raw_token = "valid-token"
    client._token_expires_mono = 999999999.0
    client._community_name = "測試社區"

    async def fake_get(self, url: str, params=None, headers=None):
        req = httpx.Request("GET", url, params=params)
        if "Announcement/Community/List" in url:
            # 10 raw items: 1 platform ad (ComId="") + 9 community announcements
            anns = [{"Id": "ad-1", "ComId": "", "Title": "平台團購廣告"}] + [
                {
                    "Id": f"ann-{i}",
                    "ComId": "25122307",
                    "Title": f"社區公告 {i}",
                    "CreateDate": "2026-10-27",
                }
                for i in range(9)
            ]
            return httpx.Response(200, json={"Data": {"Announcements": anns}}, request=req)
        if "Announcement/Detail" in url:
            if params and params.get("id") == "2610272893":
                return httpx.Response(
                    404,
                    json={
                        "Status": 0,
                        "ErrorMessage": {"Code": 404000, "Description": "公告已下架"},
                    },
                    request=req,
                )
        if "Message/Notification/List" in url:
            return httpx.Response(
                200,
                json={
                    "Data": {
                        "Messages": [
                            {
                                "MessageType": "CommunityPushNotification",
                                "CommunityPushNotificationId": "590127",
                                "Community": "測試社區",
                                "Content": "有附圖的個人通知",
                                "Files": [
                                    {
                                        "Url": "https://example.com/photo.jpg",
                                        "MimeType": "image/jpeg",
                                    }
                                ],
                            }
                        ]
                    }
                },
                request=req,
            )
        return httpx.Response(200, json={"Data": {}}, request=req)

    async def fake_post(self, url: str, json=None, headers=None):
        req = httpx.Request("POST", url)
        if "NotificationBox/LoadNotices" in url:
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": "rem-notif-1",
                        "PushingTime": "2026-11-12T10:00:00Z",
                        "HasRead": False,
                        "Message": {
                            "Title": "個人通知",
                            "Body": "有附圖的個人通知",
                            "Data": {
                                "FeatureName": "CommunityPushNotification",
                                "Id": "590127",
                            },
                        },
                    }
                ],
                request=req,
            )
        return httpx.Response(200, json={}, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # 1. Verify get_announcements keeps has_more=True even when 1 ad is filtered out of 10 raw items
    ann_res = asyncio.run(client.get_announcements(offset=0, max_count=10))
    assert len(ann_res["items"]) == 9
    assert ann_res["has_more"] is True

    # 2. Verify get_announcement_detail handles 404 (404000 公告已下架) gracefully
    det_res = asyncio.run(client.get_announcement_detail("2610272893"))
    assert det_res["is_deleted"] is True
    assert det_res["error_description"] == "公告已下架"

    # 3. Verify get_my_reminders enriches CommunityPushNotification with image files
    rem_res = asyncio.run(client.get_my_reminders())
    assert len(rem_res["items"]) == 1
    assert rem_res["items"][0]["files"] == [
        {"url": "https://example.com/photo.jpg", "mime_type": "image/jpeg"}
    ]


def test_fastapi_announcement_detail_fallback_and_path_traversal_guard(tmp_path: Path, monkeypatch):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)

    # Path traversal guard must block '..' and '/'
    assert svc.get_attachment_file_path("../etc", "passwd") is None
    assert svc.get_attachment_file_path("101", "../secret.txt") is None

    # Store announcement 101 locally with attachment
    svc.sync_messages(
        announcements=[{"id": "101", "title": "本地已備份公告", "content": "備份內文"}]
    )
    svc.save_announcement_detail_and_attachments(
        announcement_id="101",
        detail={
            "id": "101",
            "title": "本地已備份公告",
            "content": "備份內文",
            "attachments": [
                {"index": 1, "url": "https://example.com/a.pdf", "type": "pdf", "filename": "a.pdf"}
            ],
        },
        downloaded_files={1: ("1_a.pdf", b"%PDF-1.4")},
    )

    async def fake_detail_404(aid: str):
        return {
            "id": aid,
            "title": "",
            "type_name": "公告",
            "create_date": "",
            "content": "",
            "attachments": [],
            "attachment_count": 0,
            "is_deleted": True,
            "error_description": "公告已下架",
        }

    monkeypatch.setattr(main_mod.client, "get_announcement_detail", fake_detail_404)

    # Case A: Announcement 101 was backed up locally before 404 -> returns local backup
    res_backed_up = asyncio.run(main_mod.get_announcement_detail("101"))
    assert res_backed_up["is_deleted"] is True
    assert res_backed_up["title"] == "本地已備份公告"
    assert res_backed_up["content"] == "備份內文"
    assert res_backed_up["attachments"][0]["url"] == "/api/attachments/101/1_a.pdf"

    # Case B: Announcement 2610272893 was unpublished before service deployed (not in messages.json)
    res_unbacked = asyncio.run(main_mod.get_announcement_detail("2610272893"))
    assert res_unbacked["is_deleted"] is True
    assert res_unbacked["error_description"] == "公告已下架"


def test_frontend_ui_conventions_tabs_filters_infinite_scroll_and_emoji_spacing():
    index_path = (
        Path(__file__).resolve().parent.parent / "src" / "smartdaily_web" / "static" / "index.html"
    )
    html_text = index_path.read_text(encoding="utf-8")

    # 1. Only announcements and reminders tabs exist (personal tab removed)
    assert 'data-tab="announcements"' in html_text
    assert 'data-tab="reminders"' in html_text
    assert 'data-tab="personal"' not in html_text

    # 2. Reminder category filter chips exist
    for filter_key in ("all", "postal", "Col", "Ann", "CommunityPushNotification", "other"):
        assert f'data-filter="{filter_key}"' in html_text

    # 3. Infinite scroll sentinel & observer exist (manual loadMoreBtn removed)
    assert 'id="scrollSentinel"' in html_text
    assert "IntersectionObserver" in html_text
    assert "maybeAutoLoadMore" in html_text
    assert 'id="loadMoreBtn"' not in html_text

    # 4. Emoji spacing: strictly 1 half-width space in '🗑️ 已回收', never double-space or zero-space
    assert "🗑️ 已回收" in html_text
    assert "🗑  已回收" not in html_text
    assert "🗑️  已回收" not in html_text
    assert "🗑️已回收" not in html_text
