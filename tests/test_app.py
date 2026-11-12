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


def test_startup_skips_legacy_messages_but_preserves_device_subscriptions(tmp_path: Path, caplog):
    import json
    import sqlite3

    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    messages_path = tmp_path / "messages.json"
    legacy_reminder = {
        "uid": "reminder:r-legacy",
        "type": "reminder",
        "id": "r-legacy",
        "title": "📦 [郵務包裹] 舊包裹提醒",
        "body": "包裹已送達",
        "url": "/?tab=reminders",
        "tag": "rem-r-legacy",
        "discovered_at": "2026-11-10T10:00:00+00:00",
        "is_deleted": False,
        "item": {
            "id": "r-legacy",
            "pushing_time": "2026-11-10T09:00:00Z",
            "has_read": False,
            "title": "舊包裹提醒",
            "body": "包裹已送達",
            "feature_name": "postal",
            "target_id": "",
            "files": [],
            "is_deleted": False,
        },
    }
    messages_path.write_text(
        json.dumps(
            {
                "initialized_types": ["reminder"],
                "messages": [legacy_reminder],
                "metadata": {
                    "community_name": "測試社區",
                    "com_id": "123",
                    "last_full_sync_at": "2026-11-10T10:00:00+00:00",
                },
            }
        ),
        encoding="utf-8",
    )
    legacy_device = {
        "device_id": "legacy-device",
        "endpoint": "https://web.push.apple.com/legacy-device",
        "keys": {"p256dh": "key", "auth": "secret"},
        "user_agent": "test device",
        "subscribed_at": "2026-11-10T08:00:00+00:00",
        "pushed_message_uids": ["reminder:r-legacy"],
    }
    device_path = devices_dir / "legacy-device.json"
    device_path.write_text(json.dumps(legacy_device), encoding="utf-8")

    svc = PushService(data_dir=tmp_path)

    assert svc.database_file.is_file()
    assert svc.get_store_metadata() == {}
    assert svc.get_messages() == []
    assert svc.get_subscriptions()[0]["endpoint"] == legacy_device["endpoint"]
    assert svc.get_subscriptions()[0]["pushed_message_uids"] == []
    assert messages_path.is_file()
    assert not device_path.exists()
    assert (tmp_path / "vapid_keys.json").is_file()
    assert "Skipped importing legacy messages.json" in caplog.text
    assert "Imported 1 legacy device subscription" in caplog.text
    assert "Removed 1 legacy device subscription file" in caplog.text

    svc = PushService(data_dir=tmp_path)
    assert svc.get_messages() == []
    assert messages_path.is_file()
    with sqlite3.connect(svc.database_file) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003"


def test_sqlite_baselines_legacy_device_without_delivery_history(tmp_path: Path):
    import json

    messages_path = tmp_path / "messages.json"
    messages_path.write_text(
        json.dumps(
            {
                "initialized_types": ["reminder"],
                "messages": [
                    {
                        "uid": "reminder:r-old",
                        "type": "reminder",
                        "id": "r-old",
                        "discovered_at": "2026-11-10T10:00:00Z",
                        "item": {
                            "id": "r-old",
                            "pushing_time": "2026-11-10T09:00:00Z",
                            "feature_name": "postal",
                            "title": "舊提醒",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    devices_dir = tmp_path / "devices"
    devices_dir.mkdir()
    (devices_dir / "legacy-device.json").write_text(
        json.dumps(
            {
                "device_id": "legacy-device",
                "endpoint": "https://web.push.apple.com/legacy-without-delivery-history",
                "keys": {"p256dh": "key", "auth": "secret"},
            }
        ),
        encoding="utf-8",
    )

    svc = PushService(data_dir=tmp_path)

    assert svc.get_subscriptions()[0]["pushed_message_uids"] == []
    assert messages_path.exists()
    assert not (devices_dir / "legacy-device.json").exists()


def test_sqlite_round_trips_typed_messages_and_attachments(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    archive_attachment = {"name": "notice.pdf", "local_url": "/api/attachments/a-1/notice.pdf"}
    source_attachment = {"url": "https://example.test/photo.jpg", "mime_type": "image/jpeg"}
    messages = [
        {
            "uid": "announcement:a-1",
            "type": "announcement",
            "id": "a-1",
            "discovered_at": "2026-11-10T10:00:00Z",
            "is_deleted": False,
            "attachments_cached": True,
            "attachments": [archive_attachment],
            "item": {
                "id": "a-1",
                "com_id": "community-1",
                "title": "社區公告",
                "content": "公告內容",
                "is_top": False,
                "files": [source_attachment],
                "is_deleted": False,
            },
        },
        {
            "uid": "reminder:r-1",
            "type": "reminder",
            "id": "r-1",
            "discovered_at": "2026-11-10T10:01:00Z",
            "is_deleted": False,
            "item": {
                "id": "r-1",
                "title": "提醒標題",
                "body": "提醒內容",
                "feature_name": "postal",
                "files": [source_attachment],
            },
        },
        {
            "uid": "personal:p-1",
            "type": "personal",
            "id": "p-1",
            "discovered_at": "2026-11-10T10:02:00Z",
            "is_deleted": False,
            "item": {
                "id": "p-1",
                "community": "測試社區",
                "content": "個人通知內容",
                "push_date": "2026-11-10T10:02:00Z",
            },
        },
    ]
    svc.save_message_store(
        {
            "initialized_types": ["announcement", "reminder", "personal"],
            "messages": messages,
            "metadata": {},
        }
    )

    stored = {message["uid"]: message for message in svc.database.load_message_store()["messages"]}
    announcement = stored["announcement:a-1"]
    assert announcement["item"]["com_id"] == "community-1"
    assert announcement["item"]["files"] == [source_attachment]
    assert announcement["attachments"] == [archive_attachment]
    assert announcement["attachments_cached"] is True
    assert stored["reminder:r-1"]["item"]["pushing_time"] == ""
    assert stored["reminder:r-1"]["item"]["files"] == [source_attachment]
    assert stored["personal:p-1"]["item"]["content"] == "個人通知內容"


def test_startup_flattens_existing_announcement_attachment_files(tmp_path: Path, caplog):
    import sqlite3

    svc = PushService(data_dir=tmp_path)
    svc.sync_messages(announcements=[{"id": "legacy-announcement", "title": "舊公告"}])
    store = svc.get_message_store()
    message = store["messages"][0]
    message["attachments_cached"] = True
    message["attachments"] = [
        {
            "filename": "notice.pdf",
            "local_file": "1_notice.pdf",
            "url": "/api/attachments/legacy-announcement/1_notice.pdf",
            "type": "pdf",
        }
    ]
    svc.save_message_store(store)
    legacy_path = tmp_path / "attachments" / "legacy-announcement" / "1_notice.pdf"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"%PDF-legacy")
    with sqlite3.connect(svc.database_file) as connection:
        connection.execute(
            "UPDATE message_attachments SET original_name='', mime_type='', stored_filename=NULL "
            "WHERE purpose='archive'"
        )

    migrated = PushService(data_dir=tmp_path)
    detail = migrated.get_stored_announcement_detail("legacy-announcement")
    assert detail is not None
    attachment = detail["attachments"][0]
    stored_filename = attachment["url"].rsplit("/", 1)[-1]
    assert attachment["url"] == f"/api/attachments/{stored_filename}"
    assert attachment["filename"] == "notice.pdf"
    assert not legacy_path.exists()
    flattened_path = migrated.get_attachment_file_path(stored_filename)
    assert flattened_path is not None
    assert flattened_path.parent == tmp_path / "attachments"
    assert flattened_path.read_bytes() == b"%PDF-legacy"
    assert "Flattened legacy attachment storage: moved=1" in caplog.text


def test_sqlite_preserves_message_order_after_incremental_sync(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    svc.sync_messages(
        reminders=[
            {
                "id": "r-old",
                "pushing_time": "2026-11-10T10:00:00Z",
                "feature_name": "postal",
                "title": "舊提醒",
            }
        ]
    )
    svc.sync_messages(
        reminders=[
            {
                "id": "r-new",
                "pushing_time": "2026-11-10T10:01:00Z",
                "feature_name": "postal",
                "title": "新提醒",
            },
            {
                "id": "r-old",
                "pushing_time": "2026-11-10T10:00:00Z",
                "feature_name": "postal",
                "title": "舊提醒",
            },
        ]
    )

    assert [message["id"] for message in svc.get_messages()] == ["r-new", "r-old"]


def test_sqlalchemy_schema_migrations_log_and_run_idempotently(tmp_path: Path, caplog):
    import sqlite3

    svc = PushService(data_dir=tmp_path)
    assert "Applying Alembic schema migrations" in caplog.text
    assert "Alembic schema migration complete" in caplog.text

    with sqlite3.connect(svc.database_file) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003"

    caplog.clear()
    PushService(data_dir=tmp_path)
    assert "Alembic schema is current" in caplog.text


def test_alembic_schema_has_all_columns_defined_by_orm_models(tmp_path: Path):
    from sqlalchemy import inspect

    from smartdaily_web.models import Base

    svc = PushService(data_dir=tmp_path)
    inspector = inspect(svc.database.engine)

    assert set(inspector.get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)
    for table in Base.metadata.sorted_tables:
        actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
        expected_columns = set(table.columns.keys())
        assert actual_columns == expected_columns, table.name


def test_alembic_adopts_existing_sqlite_data_without_losing_subscriptions(tmp_path: Path, caplog):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    database_file = tmp_path / "smartdaily.sqlite3"
    repo_root = Path(__file__).resolve().parent.parent
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_file}")
    command.upgrade(config, "0001")

    with sqlite3.connect(database_file) as connection:
        connection.execute("DROP TABLE message_attachments")
        connection.execute(
            "CREATE TABLE message_attachments ("
            "message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE, "
            "purpose TEXT NOT NULL CHECK (purpose IN ('source', 'archive')), "
            "position INTEGER NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', "
            "PRIMARY KEY(message_id, purpose, position))"
        )
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("DROP TABLE alembic_version")
        connection.execute(
            "INSERT INTO schema_migrations VALUES (2, 'import_legacy_json', '2026-11-10T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO messages(type, external_id, discovered_at, sort_order) "
            "VALUES ('reminder', 'kept-in-sqlite', '2026-11-10T10:00:00Z', 0)"
        )
        connection.execute(
            "INSERT INTO reminders(message_id, title, body, feature_name) "
            "VALUES (1, '保留的提醒', '保留內容', 'postal')"
        )
        connection.execute(
            "INSERT INTO devices(device_id, endpoint, p256dh, auth, subscribed_at) "
            "VALUES ('old-device', 'https://web.push.apple.com/old-db-device', 'key', 'auth', "
            "'2026-11-10T09:00:00Z')"
        )
        connection.execute(
            "INSERT INTO push_deliveries(device_id, message_id, status) "
            "VALUES ('old-device', 1, 'sent')"
        )
        connection.execute(
            "UPDATE sync_state SET last_full_sync_at='2026-11-13T00:00:00+00:00' "
            "WHERE singleton_id=1"
        )

    svc = PushService(data_dir=tmp_path)

    assert [message["uid"] for message in svc.get_messages()] == ["reminder:kept-in-sqlite"]
    assert svc.get_subscriptions()[0]["endpoint"] == "https://web.push.apple.com/old-db-device"
    assert svc.get_subscriptions()[0]["pushed_message_uids"] == ["reminder:kept-in-sqlite"]
    assert "last_full_sync_at" not in svc.get_store_metadata()
    assert "Cleared last full sync time" in caplog.text
    with sqlite3.connect(database_file) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003"


def test_malformed_legacy_messages_json_is_ignored_and_retained(tmp_path: Path, caplog):
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("{invalid json", encoding="utf-8")

    svc = PushService(data_dir=tmp_path)

    assert svc.get_messages() == []
    assert messages_path.exists()
    assert "Skipped importing legacy messages.json" in caplog.text


def test_successful_full_sync_removes_legacy_messages_json(tmp_path: Path, monkeypatch, caplog):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)
    monkeypatch.setattr(main_mod, "poll_lock", asyncio.Lock())
    monkeypatch.setattr(
        main_mod,
        "poll_status",
        {"last_poll_at": None, "last_trace_id": None, "last_error": None, "is_running": False},
    )
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("old messages", encoding="utf-8")

    async def fetch_announcements():
        return ({"community_name": "測試社區", "com_id": "123"}, [])

    async def fetch_reminders():
        return []

    monkeypatch.setattr(main_mod, "fetch_all_announcements", fetch_announcements)
    monkeypatch.setattr(main_mod, "fetch_all_reminders", fetch_reminders)

    asyncio.run(main_mod.poll_once())

    assert not messages_path.exists()
    assert "Removed legacy messages.json after successful full sync" in caplog.text


def test_failed_full_sync_keeps_legacy_messages_json(tmp_path: Path, monkeypatch):
    import asyncio
    import importlib

    import pytest

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)
    monkeypatch.setattr(main_mod, "poll_lock", asyncio.Lock())
    monkeypatch.setattr(
        main_mod,
        "poll_status",
        {"last_poll_at": None, "last_trace_id": None, "last_error": None, "is_running": False},
    )
    messages_path = tmp_path / "messages.json"
    messages_path.write_text("old messages", encoding="utf-8")

    async def fail_fetch_announcements():
        raise RuntimeError("SmartDaily unavailable")

    async def fetch_reminders():
        return []

    monkeypatch.setattr(main_mod, "fetch_all_announcements", fail_fetch_announcements)
    monkeypatch.setattr(main_mod, "fetch_all_reminders", fetch_reminders)

    with pytest.raises(RuntimeError, match="SmartDaily unavailable"):
        asyncio.run(main_mod.poll_once())

    assert messages_path.is_file()


def test_app_lifespan_runs_migrations_before_starting_background_poll(monkeypatch):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")
    calls: list[str] = []
    monkeypatch.delenv("DISABLE_BACKGROUND_POLL", raising=False)

    async def fake_background_poll_loop():
        return None

    class FakeTask:
        def cancel(self):
            calls.append("cancel")

    def fake_create_task(coro):
        calls.append("start_poll")
        coro.close()
        return FakeTask()

    monkeypatch.setattr(main_mod.push_service, "migrate", lambda: calls.append("migrate"))
    monkeypatch.setattr(main_mod, "background_poll_loop", fake_background_poll_loop)
    monkeypatch.setattr(main_mod.asyncio, "create_task", fake_create_task)

    async def scenario():
        async with main_mod.lifespan(main_mod.app):
            assert calls == ["migrate", "start_poll"]
        assert calls == ["migrate", "start_poll", "cancel"]

    asyncio.run(scenario())


def test_test_push_sends_only_to_the_requested_device(tmp_path: Path, monkeypatch):
    import json

    import smartdaily_web.push_service as push_module

    svc = PushService(data_dir=tmp_path)
    first = {
        "endpoint": "https://web.push.apple.com/current-device",
        "keys": {"p256dh": "current-key", "auth": "current-auth"},
    }
    other = {
        "endpoint": "https://web.push.apple.com/other-device",
        "keys": {"p256dh": "other-key", "auth": "other-auth"},
    }
    svc.add_subscription(first)
    svc.add_subscription(other)
    delivered_to: list[str] = []

    def fake_webpush(**kwargs):
        delivered_to.append(kwargs["subscription_info"]["endpoint"])
        assert json.loads(kwargs["data"])["title"] == "測試推播"

    monkeypatch.setattr(push_module, "webpush", fake_webpush)

    result = svc.send_push_to_device(
        endpoint=first["endpoint"],
        title="測試推播",
        body="只送目前裝置",
    )

    assert result == {"sent": 1, "failed": 0, "active_subscribers": 1}
    assert delivered_to == [first["endpoint"]]


def test_test_push_returns_not_found_for_an_unknown_device(tmp_path: Path, monkeypatch):
    import asyncio
    import importlib

    import pytest
    from fastapi import HTTPException

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            main_mod.send_test_push(
                main_mod.TestPushPayload(endpoint="https://web.push.apple.com/unknown")
            )
        )

    assert exc_info.value.status_code == 404


def test_push_service_vapid_and_per_device_push_tracking(tmp_path: Path, monkeypatch):
    import sqlite3

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

    # Subscribe Device B after its baseline is already stored in SQLite
    sub_b = {
        "endpoint": "https://web.push.apple.com/device-b",
        "keys": {"p256dh": "kb", "auth": "ab"},
    }
    svc.add_subscription(sub_b, user_agent="iPad-B")
    subs_with_b = svc.get_subscriptions()
    dev_b = next(s for s in subs_with_b if s["endpoint"] == sub_b["endpoint"])
    assert dev_b["pushed_message_uids"] == []

    # Second run: new announcement & 2 new reminders arrive
    # Announcements are stored for display/backup; reminder pushes put the category emoji first.
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
    assert events2[0]["title"] == "📦 [郵務包裹] 包裹到件通知"
    assert events2[0]["url"] == "/?tab=reminders"
    assert events2[1]["title"] == "📢 [社區公告] 新公告通知"
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
    with sqlite3.connect(svc.database_file) as connection:
        delivery = connection.execute(
            """
            SELECT d.status, d.attempts FROM push_deliveries d
            JOIN devices ON devices.device_id=d.device_id
            JOIN messages ON messages.message_id=d.message_id
            WHERE devices.endpoint=? AND messages.external_id='301'
            """,
            (sub_b["endpoint"],),
        ).fetchone()
    assert delivery == ("failed", 1)
    svc.save_device_subscription(dev_a_state)
    with sqlite3.connect(svc.database_file) as connection:
        delivery = connection.execute(
            """
            SELECT d.status, d.attempts FROM push_deliveries d
            JOIN devices ON devices.device_id=d.device_id
            JOIN messages ON messages.message_id=d.message_id
            WHERE devices.endpoint=? AND messages.external_id='301'
            """,
            (sub_a["endpoint"],),
        ).fetchone()
    assert delivery == ("sent", 1)

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
    with sqlite3.connect(svc.database_file) as connection:
        delivery = connection.execute(
            """
            SELECT d.status, d.attempts, d.sent_at FROM push_deliveries d
            JOIN devices ON devices.device_id=d.device_id
            JOIN messages ON messages.message_id=d.message_id
            WHERE devices.endpoint=? AND messages.external_id='301'
            """,
            (sub_b["endpoint"],),
        ).fetchone()
    assert delivery[0:2] == ("sent", 2)
    assert delivery[2]


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
    assert len(svc.get_subscriptions()) == 2
    assert svc.remove_subscription("https://web.push.apple.com/test-endpoint-1") == 1
    assert len(svc.get_subscriptions()) == 1
    assert svc.remove_subscription("https://web.push.apple.com/test-endpoint-2") == 0
    assert svc.get_subscriptions() == []


def test_old_reminder_backfill_is_baselined_and_only_newer_item_is_pushed(
    tmp_path: Path, monkeypatch
):
    svc = PushService(data_dir=tmp_path)
    subscription = {
        "endpoint": "https://web.push.apple.com/backfill-device",
        "keys": {"p256dh": "key", "auth": "auth"},
    }
    svc.add_subscription(subscription)
    device = svc.get_subscriptions()[0]
    device["subscribed_at"] = "2026-11-10T10:09:30Z"
    svc.save_device_subscription(device)

    def reminder(item_id: str, minute: int) -> dict[str, str]:
        return {
            "id": item_id,
            "pushing_time": f"2026-11-10T10:{minute:02d}:00.000000Z",
            "feature_name": "postal",
            "title": item_id,
            "body": item_id,
        }

    svc.sync_messages(reminders=[reminder("r-10", 10)], full_sync=True)
    events = svc.sync_messages(
        reminders=[
            reminder("r-11", 11),
            reminder("r-10", 10),
            reminder("r-09", 9),
            reminder("r-08", 8),
        ],
        full_sync=True,
    )

    assert [event["id"] for event in events] == ["r-11"]
    device = svc.get_subscriptions()[0]
    pushed_uids = set(device["pushed_message_uids"])
    assert "reminder:r-08" in pushed_uids
    assert "reminder:r-09" in pushed_uids
    assert "reminder:r-11" not in pushed_uids

    delivered: list[str] = []
    monkeypatch.setattr(
        svc,
        "_send_single_push",
        lambda _sub, message: delivered.append(message["uid"]) or "sent",
    )
    result = svc.push_unpushed_messages()
    assert result["sent"] == 1
    assert delivered == ["reminder:r-11"]


def test_device_create_time_blocks_older_reminders_even_if_uid_is_missing(
    tmp_path: Path, monkeypatch
):
    svc = PushService(data_dir=tmp_path)
    old_reminder = {
        "id": "r-before-device",
        "pushing_time": "2020-01-01T00:00:00Z",
        "feature_name": "postal",
        "title": "舊提醒",
        "body": "訂閱前的提醒",
    }
    svc.sync_messages(reminders=[old_reminder], full_sync=True)
    svc.add_subscription(
        {
            "endpoint": "https://web.push.apple.com/create-time-device",
            "keys": {"p256dh": "key", "auth": "auth"},
        }
    )

    # Simulate an older device record with a create_time field and missing UID data.
    device = svc.get_subscriptions()[0]
    device.pop("subscribed_at")
    device["create_time"] = "2026-11-10T10:00:00Z"
    device["pushed_message_uids"] = []
    svc.save_device_subscription(device)

    new_reminder = {
        "id": "r-after-device",
        "pushing_time": "2026-11-10T10:01:00Z",
        "feature_name": "postal",
        "title": "新提醒",
        "body": "訂閱後的提醒",
    }
    svc.sync_messages(reminders=[new_reminder, old_reminder], full_sync=True)

    delivered: list[str] = []
    monkeypatch.setattr(
        svc,
        "_send_single_push",
        lambda _sub, message: delivered.append(message["uid"]) or "sent",
    )
    result = svc.push_unpushed_messages()

    assert result["sent"] == 1
    assert delivered == ["reminder:r-after-device"]


def test_new_subscription_baselines_only_messages_without_discovery_time(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    svc.sync_messages(
        reminders=[
            {
                "id": "known-time",
                "pushing_time": "2026-11-10T10:00:00Z",
                "feature_name": "postal",
                "title": "可判斷時間的提醒",
            },
            {
                "id": "legacy-time",
                "pushing_time": "2026-11-10T10:00:00Z",
                "feature_name": "postal",
                "title": "舊格式提醒",
            },
        ]
    )
    store = svc.get_message_store()
    legacy_message = next(msg for msg in store["messages"] if msg["id"] == "legacy-time")
    legacy_message.pop("discovered_at")
    svc.save_message_store(store)

    svc.add_subscription(
        {
            "endpoint": "https://web.push.apple.com/partial-baseline-device",
            "keys": {"p256dh": "key", "auth": "auth"},
        }
    )

    device = svc.get_subscriptions()[0]
    assert device["pushed_message_uids"] == ["reminder:legacy-time"]


def test_sqlite_metadata_and_local_pagination(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    svc.sync_messages(
        announcements=[
            {"id": "2", "title": "普通公告 2"},
            {"id": "3", "title": "置頂公告", "is_top": True},
            {"id": "1", "title": "普通公告 1"},
        ],
        reminders=[
            {"id": "r-1", "pushing_time": "2026-11-10T10:01:00Z", "body": "較舊"},
            {"id": "r-2", "pushing_time": "2026-11-10T10:02:00Z", "body": "較新"},
        ],
    )
    svc.update_store_metadata(community_name="測試社區", com_id="123")

    first_page, total = svc.get_message_page("announcement", offset=0, max_count=2)
    second_page, _ = svc.get_message_page("announcement", offset=2, max_count=2)
    reminder_page, reminder_total = svc.get_message_page("reminder", offset=0, max_count=10)
    saved = svc.get_message_store()

    assert [item["id"] for item in first_page] == ["3", "2"]
    assert [item["id"] for item in second_page] == ["1"]
    assert total == 3
    assert [item["id"] for item in reminder_page] == ["r-2", "r-1"]
    assert reminder_total == 2
    assert saved["metadata"]["community_name"] == "測試社區"


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

    # A full background sync marks announcement 101 as recalled; display merging is read-only.
    svc.sync_messages(
        announcements=[{"id": "100", "title": "正常公告", "content": "正常保留"}],
        full_sync=True,
    )
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
    stored_filename = saved_atts[0]["url"].rsplit("/", 1)[-1]
    assert saved_atts[0]["url"] == f"/api/attachments/{stored_filename}"
    assert len(stored_filename) == 36
    assert (tmp_path / "attachments" / stored_filename).is_file()
    local_path = svc.get_attachment_file_path(stored_filename)
    assert local_path is not None and local_path.read_bytes() == b"%PDF-1.4 dummy"

    detail_with_att = svc.get_stored_announcement_detail("101")
    assert detail_with_att is not None
    assert detail_with_att["attachment_count"] == 1
    assert detail_with_att["attachments"][0]["url"] == f"/api/attachments/{stored_filename}"
    assert detail_with_att["attachments"][0]["filename"] == "notice.pdf"


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
    assert any("Polled 1 announcements and 1 reminders" in m for m in messages)
    assert any("Poll completed:" in m for m in messages)


def test_poll_once_fetches_all_announcement_and_reminder_pages(tmp_path: Path, monkeypatch):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)
    monkeypatch.setattr(main_mod, "poll_lock", asyncio.Lock())
    monkeypatch.setattr(
        main_mod,
        "poll_status",
        {
            "last_poll_at": None,
            "last_trace_id": None,
            "last_error": None,
            "is_running": False,
            "poll_interval_seconds": 180,
        },
    )

    announcement_offsets: list[int] = []

    async def fake_get_announcements(offset: int = 0, max_count: int = 10):
        announcement_offsets.append(offset)
        if offset == 0:
            return {
                "community_name": "測試社區",
                "com_id": "123",
                "has_more": True,
                "items": [{"id": "102", "title": "較新公告"}],
            }
        return {"has_more": False, "items": [{"id": "101", "title": "較舊公告"}]}

    reminder_cursors: list[str] = []

    async def fake_get_reminders(before_datetime: str = ""):
        reminder_cursors.append(before_datetime)
        if not before_datetime:
            return {
                "has_more": True,
                "next_before_datetime": "2026-11-10T10:01:00Z",
                "items": [
                    {
                        "id": "r-2",
                        "pushing_time": "2026-11-10T10:02:00Z",
                        "feature_name": "postal",
                        "title": "較新提醒",
                        "body": "新包裹",
                    }
                ],
            }
        return {
            "has_more": False,
            "next_before_datetime": "",
            "items": [
                {
                    "id": "r-1",
                    "pushing_time": "2026-11-10T10:01:00Z",
                    "feature_name": "postal",
                    "title": "較舊提醒",
                    "body": "舊包裹",
                }
            ],
        }

    async def fake_cache_attachments(_announcement_id: str):
        return None

    monkeypatch.setattr(main_mod.client, "get_announcements", fake_get_announcements)
    monkeypatch.setattr(main_mod.client, "get_my_reminders", fake_get_reminders)
    monkeypatch.setattr(main_mod, "ensure_announcement_attachments_cached", fake_cache_attachments)

    result = asyncio.run(main_mod.poll_once())

    assert result["new_events_count"] == 0  # first sync establishes the historical baseline
    assert announcement_offsets == [0, 10]
    assert reminder_cursors == ["", "2026-11-10T10:01:00Z"]
    announcements, ann_total = svc.get_message_page("announcement", 0, 10)
    reminders, rem_total = svc.get_message_page("reminder", 0, 10)
    assert ann_total == 2
    assert {item["id"] for item in announcements} == {"101", "102"}
    assert rem_total == 2
    assert {item["id"] for item in reminders} == {"r-1", "r-2"}
    assert svc.get_store_metadata()["community_name"] == "測試社區"


def test_manual_poll_schedules_once_and_skips_when_running(monkeypatch):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")

    async def scenario():
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = 0

        async def fake_poll_once():
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()

        monkeypatch.setattr(main_mod, "poll_lock", asyncio.Lock())
        monkeypatch.setattr(main_mod, "manual_poll_task", None)
        monkeypatch.setattr(main_mod, "poll_once", fake_poll_once)

        first = main_mod.schedule_manual_poll()
        second = main_mod.schedule_manual_poll()
        assert first == {"started": True, "status": "scheduled"}
        assert second == {"started": False, "status": "already_running"}
        await asyncio.wait_for(started.wait(), timeout=1)
        assert calls == 1

        finish.set()
        await main_mod.manual_poll_task

        await main_mod.poll_lock.acquire()
        third = main_mod.schedule_manual_poll()
        main_mod.poll_lock.release()
        assert third == {"started": False, "status": "already_running"}

    asyncio.run(scenario())


def test_legacy_subscription_migration_and_expired_device_cleanup(
    tmp_path: Path, monkeypatch, caplog
):
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
    assert "Removed 1 legacy device subscription file" in caplog.text

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

    # Sync is performed by the background job; the display merge only reads stored state.
    svc.sync_messages(reminders=next_page1)
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


def test_partial_sync_with_short_page_keeps_older_reminders_active(tmp_path: Path):
    svc = PushService(data_dir=tmp_path)
    svc.sync_messages(
        reminders=[
            {
                "id": "old",
                "pushing_time": "2026-11-10T10:00:00Z",
                "feature_name": "postal",
                "title": "舊提醒",
            }
        ]
    )

    svc.sync_messages(
        reminders=[
            {
                "id": "new",
                "pushing_time": "2026-11-10T10:01:00Z",
                "feature_name": "postal",
                "title": "新提醒",
            }
        ],
        full_sync=False,
    )

    by_id = {message["id"]: message for message in svc.get_messages()}
    assert by_id["old"]["is_deleted"] is False


def test_poll_once_uses_incremental_pages_until_full_sync_interval(tmp_path: Path, monkeypatch):
    import asyncio
    import importlib
    from datetime import datetime, timedelta, timezone

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)
    monkeypatch.setattr(main_mod, "poll_lock", asyncio.Lock())
    monkeypatch.setattr(main_mod, "FULL_SYNC_INTERVAL_SECONDS", 86400, raising=False)
    monkeypatch.setattr(
        main_mod,
        "poll_status",
        {
            "last_poll_at": None,
            "last_trace_id": None,
            "last_error": None,
            "is_running": False,
            "poll_interval_seconds": 180,
        },
    )

    last_full_sync = datetime(2026, 11, 12, tzinfo=timezone.utc)
    current_time = [last_full_sync + timedelta(hours=23, minutes=59)]
    monkeypatch.setattr(main_mod, "_utc_now", lambda: current_time[0], raising=False)
    svc.sync_messages(
        announcements=[
            {"id": "99", "title": "較舊公告"},
            {"id": "100", "title": "既有公告"},
        ],
        reminders=[
            {
                "id": "r-1",
                "pushing_time": "2026-11-12T09:00:00Z",
                "feature_name": "postal",
                "title": "既有提醒",
            }
        ],
        full_sync=True,
    )
    svc.update_store_metadata(last_full_sync_at=last_full_sync.isoformat())

    announcement_offsets: list[int] = []
    reminder_cursors: list[str] = []
    full_sync_values: list[bool] = []
    original_sync_messages = svc.sync_messages

    def record_sync_messages(*args, **kwargs):
        full_sync_values.append(kwargs.get("full_sync", False))
        return original_sync_messages(*args, **kwargs)

    monkeypatch.setattr(svc, "sync_messages", record_sync_messages)

    async def fake_get_announcements(offset: int = 0, max_count: int = 10):
        announcement_offsets.append(offset)
        if offset == 0:
            return {
                "community_name": "測試社區",
                "com_id": "123",
                "has_more": True,
                "items": [
                    {"id": "101", "title": "新公告"},
                    {"id": "100", "title": "既有公告"},
                ],
            }
        return {"has_more": False, "items": []}

    async def fake_get_reminders(before_datetime: str = ""):
        reminder_cursors.append(before_datetime)
        if not before_datetime:
            return {
                "has_more": True,
                "next_before_datetime": "2026-11-12T10:00:00Z",
                "items": [
                    {
                        "id": "r-2",
                        "pushing_time": "2026-11-12T10:00:00Z",
                        "feature_name": "postal",
                        "title": "新提醒",
                    }
                ],
            }
        return {
            "has_more": False,
            "items": [
                {
                    "id": "r-1",
                    "pushing_time": "2026-11-12T09:00:00Z",
                    "feature_name": "postal",
                    "title": "既有提醒",
                }
            ],
        }

    async def fake_cache_attachments(_announcement_id: str):
        return None

    monkeypatch.setattr(main_mod.client, "get_announcements", fake_get_announcements)
    monkeypatch.setattr(main_mod.client, "get_my_reminders", fake_get_reminders)
    monkeypatch.setattr(main_mod, "ensure_announcement_attachments_cached", fake_cache_attachments)

    async def run_incremental_then_full_sync():
        incremental_result = await main_mod.poll_once()
        current_time[0] = last_full_sync + timedelta(days=1)
        full_result = await main_mod.poll_once()
        return incremental_result, full_result

    incremental_result, full_result = asyncio.run(run_incremental_then_full_sync())

    assert incremental_result["new_events_count"] == 1
    assert full_result["new_events_count"] == 0
    assert announcement_offsets == [0, 0, 10]
    assert reminder_cursors == ["", "", "2026-11-12T10:00:00Z"]
    assert full_sync_values == [False, True]
    assert svc.get_store_metadata()["last_full_sync_at"] == current_time[0].isoformat()
    by_uid = {message["uid"]: message for message in svc.get_messages()}
    assert by_uid["announcement:99"]["is_deleted"] is True
    assert by_uid["reminder:r-1"]["is_deleted"] is False


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


def test_fastapi_announcement_detail_reads_local_store_and_guards_paths(
    tmp_path: Path, monkeypatch
):
    import asyncio
    import importlib
    from urllib.parse import unquote

    import pytest
    from fastapi import HTTPException

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)

    async def unexpected_detail_call(_announcement_id: str):
        raise AssertionError("announcement detail endpoint must read the local archive")

    monkeypatch.setattr(main_mod.client, "get_announcement_detail", unexpected_detail_call)

    # Path traversal guard must block '..' and '/'
    assert svc.get_attachment_file_path("../secret.txt") is None

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
                {
                    "index": 1,
                    "url": "https://example.com/a.pdf",
                    "type": "pdf",
                    "filename": "社區公告.pdf",
                }
            ],
        },
        downloaded_files={1: ("1_a.pdf", b"%PDF-1.4")},
    )
    svc.sync_messages(announcements=[], full_sync=True)

    # Announcement detail and recalled state come from the local SQLite archive.
    res_backed_up = asyncio.run(main_mod.get_announcement_detail("101"))
    assert res_backed_up["is_deleted"] is True
    assert res_backed_up["title"] == "本地已備份公告"
    assert res_backed_up["content"] == "備份內文"
    stored_filename = res_backed_up["attachments"][0]["url"].rsplit("/", 1)[-1]
    assert res_backed_up["attachments"][0]["url"] == f"/api/attachments/{stored_filename}"
    response = asyncio.run(main_mod.serve_cached_attachment(stored_filename))
    assert response.media_type == "application/pdf"
    content_disposition = response.headers["content-disposition"]
    assert content_disposition.startswith("inline;")
    assert "filename*=utf-8''" in content_disposition.lower()
    assert 'filename="download.pdf"' in content_disposition
    filename_marker = "filename*=utf-8''"
    marker_position = content_disposition.lower().index(filename_marker) + len(filename_marker)
    encoded_filename = content_disposition[marker_position:]
    assert unquote(encoded_filename) == "社區公告.pdf"

    # Unknown announcements are not fetched on demand from SmartDaily.
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main_mod.get_announcement_detail("2610272893"))
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "本機尚未收錄此公告"


def test_fastapi_announcement_detail_uses_linked_reminder_when_local_copy_is_missing(
    tmp_path: Path, monkeypatch
):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)
    svc.sync_messages(
        reminders=[
            {
                "id": "r-101",
                "pushing_time": "2026-11-10T10:00:00Z",
                "feature_name": "Ann",
                "target_id": "101",
                "title": "社區公告通知",
                "body": "公告摘要內容",
            }
        ]
    )

    detail = asyncio.run(main_mod.get_announcement_detail("101"))

    assert detail["detail_unavailable"] is True
    assert detail["title"] == "社區公告通知"
    assert detail["content"] == "公告摘要內容"


def test_frontend_data_endpoints_and_test_push_use_local_database(tmp_path: Path, monkeypatch):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)
    svc.sync_messages(
        announcements=[
            {"id": "101", "title": "本地公告", "content": "公告內容"},
        ],
        reminders=[
            {
                "id": "r-1",
                "pushing_time": "2026-11-10T10:00:00Z",
                "feature_name": "postal",
                "title": "包裹到件",
                "body": "有一件包裹",
            }
        ],
    )
    svc.update_store_metadata(community_name="本地社區", com_id="123")
    current_endpoint = "https://web.push.apple.com/current-browser"
    svc.add_subscription({"endpoint": current_endpoint, "keys": {"p256dh": "key", "auth": "auth"}})

    async def unexpected_smartdaily_call(*_args, **_kwargs):
        raise AssertionError("frontend data endpoints must not call SmartDaily")

    monkeypatch.setattr(main_mod.client, "ensure_context", unexpected_smartdaily_call)
    monkeypatch.setattr(main_mod.client, "get_announcements", unexpected_smartdaily_call)
    monkeypatch.setattr(main_mod.client, "get_my_reminders", unexpected_smartdaily_call)
    monkeypatch.setattr(main_mod.client, "get_announcement_detail", unexpected_smartdaily_call)

    status = asyncio.run(main_mod.get_status())
    ann_page = asyncio.run(main_mod.list_announcements(offset=0, max_count=1))
    reminder_page = asyncio.run(main_mod.list_reminders(offset=0, max_count=1))
    detail = asyncio.run(main_mod.get_announcement_detail("101"))

    pushed: dict[str, str] = {}

    def fake_test_push(**kwargs):
        pushed.update(kwargs)
        return {"sent": 0, "failed": 0, "active_subscribers": 0}

    def unexpected_broadcast(**_kwargs):
        raise AssertionError("test push must not broadcast to every device")

    monkeypatch.setattr(svc, "send_push_to_device", fake_test_push)
    monkeypatch.setattr(svc, "send_push_to_all", unexpected_broadcast)
    test_result = asyncio.run(
        main_mod.send_test_push(main_mod.TestPushPayload(endpoint=current_endpoint))
    )

    assert status["community_name"] == "本地社區"
    assert ann_page["items"][0]["id"] == "101"
    assert reminder_page["items"][0]["id"] == "r-1"
    assert detail["content"] == "公告內容"
    assert test_result["body"] == "📢 最新公告：本地公告\n📬 最新提醒：有一件包裹..."
    assert pushed["title"].startswith("🔔 [推播測試] 本地社區")
    assert pushed["endpoint"] == current_endpoint


def test_background_detail_fetch_marks_recalled_announcement(tmp_path: Path, monkeypatch):
    import asyncio
    import importlib

    main_mod = importlib.import_module("smartdaily_web.main")
    svc = PushService(data_dir=tmp_path)
    monkeypatch.setattr(main_mod, "push_service", svc)
    svc.sync_messages(announcements=[{"id": "101", "title": "已下架公告"}])

    async def fake_detail(_announcement_id: str):
        return {"id": "101", "title": "", "content": "", "is_deleted": True}

    monkeypatch.setattr(main_mod.client, "get_announcement_detail", fake_detail)
    result = asyncio.run(main_mod.ensure_announcement_attachments_cached("101"))
    stored = svc.get_stored_announcement_detail("101")

    assert result is None
    assert stored is not None
    assert stored["is_deleted"] is True


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


def test_frontend_test_push_uses_only_the_current_browser_subscription():
    index_path = (
        Path(__file__).resolve().parent.parent / "src" / "smartdaily_web" / "static" / "index.html"
    )
    html_text = index_path.read_text(encoding="utf-8")
    test_push_function = html_text.split("async function sendTestPush() {", 1)[1].split(
        "// Event bindings", 1
    )[0]

    assert "navigator.serviceWorker.ready" in test_push_function
    assert "registration.pushManager.getSubscription()" in test_push_function
    assert "if (!subscription)" in test_push_function
    assert "JSON.stringify({ endpoint: subscription.endpoint })" in test_push_function
    assert "已發送測試推播至目前裝置" in test_push_function
    assert "data.sent} 台裝置" not in test_push_function

    # 3. Infinite scroll sentinel & observer exist (manual loadMoreBtn removed)
    assert 'id="scrollSentinel"' in html_text
    assert "IntersectionObserver" in html_text
    assert "maybeAutoLoadMore" in html_text
    assert 'id="loadMoreBtn"' not in html_text
    assert "fetch(`/api/announcements?offset=${currentOffset}&max_count=10`)" in html_text
    assert "fetch(`/api/notifications/reminders?offset=${currentOffset}&max_count=10`)" in html_text
    assert "detail.detail_unavailable" in html_text
    assert 'id="refreshBtn"' in html_text
    assert ">⚡ 立即更新</button>" in html_text
    assert 'id="pollNowBtn"' not in html_text
    assert "立即檢查新訊息" not in html_text
    refresh_handler = html_text.split(
        'document.getElementById("refreshBtn").addEventListener("click", async () => {'
    )[1].split("    });", 1)[0]
    assert 'fetch("/api/poll-now", { method: "POST" })' in refresh_handler

    # 4. Emoji spacing: strictly 1 half-width space in '🗑️ 已回收', never double-space or zero-space
    assert "🗑️ 已回收" in html_text
    assert "🗑  已回收" not in html_text
    assert "🗑️  已回收" not in html_text
    assert "🗑️已回收" not in html_text
