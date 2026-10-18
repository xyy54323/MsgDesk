from __future__ import annotations

import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.config import AppConfig, NotificationChannelConfig, QuietHours
from msgdesk.db import Database
from msgdesk.errors import AppError
from msgdesk.repository import Repository
from msgdesk.services.notifications import (
    BarkChannel,
    NotificationService,
    PushPlusChannel,
    ServerChanChannel,
    WecomBotChannel,
    build_channel,
    render_message,
)
from msgdesk.timeutil import iso_utc


class _PushHandler(BaseHTTPRequestHandler):
    records: list[dict] = []
    status = 200
    response: dict = {"code": 0}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = raw.decode("utf-8", errors="replace")
        type(self).records.append(
            {"path": self.path, "body": body, "headers": dict(self.headers)}
        )
        payload = json.dumps(type(self).response).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        return None


class PushChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        _PushHandler.records = []
        _PushHandler.status = 200
        _PushHandler.response = {"code": 0}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _PushHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def payload() -> dict:
        return {
            "type": "message",
            "message_id": "msg-1",
            "priority": "urgent",
            "urgency": "urgent",
            "category": "学校通知",
            "source": "教务处",
            "conversation": "三班群",
            "summary": "明天 18:00 前完成报名",
            "content": "请各位同学明天 18:00 前完成报名，逾期不再受理。",
            "deadline_at": "2026-10-20T10:00:00Z",
            "source_url": "https://example.com/notice",
        }

    def test_four_channel_request_shapes(self) -> None:
        PushPlusChannel("push-token", self.base + "/pushplus").send(self.payload())
        ServerChanChannel("SCT-key", self.base + "/{sendkey}.send").send(self.payload())
        WecomBotChannel(self.base + "/wecom").send(self.payload())
        BarkChannel("device-key", self.base + "/bark", group="MsgDesk").send(self.payload())

        self.assertEqual(
            [record["path"] for record in _PushHandler.records],
            ["/pushplus", "/SCT-key.send", "/wecom", "/bark"],
        )
        pushplus = _PushHandler.records[0]["body"]
        self.assertEqual(pushplus["token"], "push-token")
        self.assertEqual(pushplus["template"], "markdown")
        self.assertTrue(pushplus["title"].startswith("[紧急] 教务处："))
        self.assertIn("消息 ID：msg-1", pushplus["content"])

        serverchan = _PushHandler.records[1]["body"]
        self.assertIn("title", serverchan)
        self.assertIn("desp", serverchan)

        wecom = _PushHandler.records[2]["body"]
        self.assertEqual(wecom["msgtype"], "markdown")
        self.assertIn("**", wecom["markdown"]["content"])

        bark = _PushHandler.records[3]["body"]
        self.assertEqual(bark["device_key"], "device-key")
        self.assertEqual(bark["level"], "critical")
        self.assertEqual(bark["group"], "MsgDesk")
        self.assertIn("body", bark)

    def test_non_2xx_maps_to_remote_error(self) -> None:
        _PushHandler.status = 500
        with self.assertRaises(AppError) as ctx:
            PushPlusChannel("push-token", self.base + "/pushplus").send(self.payload())
        self.assertEqual(ctx.exception.code, "REMOTE_TEMPORARY_ERROR")

    def test_service_error_code_is_rejected(self) -> None:
        _PushHandler.response = {"code": 999, "msg": "bad token"}
        with self.assertRaises(AppError) as ctx:
            ServerChanChannel("SCT-key", self.base + "/{sendkey}.send").send(self.payload())
        self.assertEqual(ctx.exception.code, "REMOTE_TEMPORARY_ERROR")

    def test_plain_text_response_is_success(self) -> None:
        _PushHandler.response = {"code": 0}
        _PushHandler.status = 200

        class PlainHandler(_PushHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                payload = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), PlainHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            BarkChannel(
                "device-key", f"http://127.0.0.1:{server.server_address[1]}/bark"
            ).send(self.payload())
        finally:
            server.shutdown()
            server.server_close()

    def test_render_title_and_empty_fallbacks(self) -> None:
        long_text = "很长的摘要" * 20
        title, _ = render_message(
            {"type": "message", "source": "来源", "summary": long_text, "priority": "urgent"}
        )
        self.assertLessEqual(len(title), 60)
        self.assertTrue(title.startswith("[紧急] 来源："))

        title, _ = render_message(
            {"type": "message", "source": "来源", "summary": "内容", "priority": "high"}
        )
        self.assertTrue(title.startswith("[重要] 来源："))

        title, _ = render_message({"type": "message", "source": "来源"})
        self.assertIn("新消息", title)

        title, body = render_message(
            {"type": "message", "source": "来源", "content": "正文" * 80}
        )
        self.assertLessEqual(len(title), 60)
        self.assertIn("正文正文", body)

    def test_credentials_are_not_leaked_in_errors(self) -> None:
        _PushHandler.status = 500
        with self.assertRaises(AppError) as ctx:
            PushPlusChannel(
                "super-secret-token", self.base + "/pushplus"
            ).send(self.payload())
        self.assertNotIn("super-secret-token", str(ctx.exception))
        self.assertNotIn("super-secret-token", json.dumps(ctx.exception.to_dict(),
                                                         ensure_ascii=False))

    def test_channel_level_priority_and_urgent_only(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = AppConfig()
            config.base_dir = root
            config.system.data_dir = str(root / "data")
            config.system.digest_dir = str(root / "digests")
            config.system.require_auth = False
            config.scheduling.quiet_hours = QuietHours(start="03:00", end="03:01")
            config.notifications.min_priority = "normal"
            config.notifications.channels = [
                NotificationChannelConfig(
                    id="mock-urgent",
                    type="mock",
                    enabled=True,
                    config={"min_priority": "urgent"},
                ),
                NotificationChannelConfig(
                    id="mock-only-urgent",
                    type="mock",
                    enabled=True,
                    config={"notify_urgent_only": True},
                ),
            ]
            config.ensure_dirs()
            db = Database(root / "data" / "messages.db")
            db.migrate()
            repo = Repository(db)
            source = repo.create_source({
                "type": "rss",
                "name": "测试源",
                "adapter_type": "rss",
                "config": {"url": "https://example.com/feed.xml"},
            })
            with db.connection() as conn:
                conversation_id = repo.upsert_conversation(
                    conn, source["id"], "g1", "测试群", "group"
                )
                message_id = repo.insert_message(conn, {
                    "source_id": source["id"],
                    "conversation_id": conversation_id,
                    "platform_message_id": "p1",
                    "published_at": iso_utc(),
                    "content_text": "请完成报名",
                    "content_hash": hashlib.sha1(b"test").hexdigest(),
                })
            repo.override_processing(
                message_id,
                {"priority": "high", "urgency": "normal", "summary_short": "报名提醒"},
            )
            service = NotificationService(config, repo)
            high = service.notify_message(message_id, "mock-urgent")
            self.assertEqual(high["status"], "suppressed")
            self.assertEqual(high["reason"], "priority_below_threshold")
            only_urgent = service.notify_message(message_id, "mock-only-urgent")
            self.assertEqual(only_urgent["reason"], "urgent_only")

    def test_build_channel_requires_credentials(self) -> None:
        with TemporaryDirectory() as tmp:
            for channel_type in ("pushplus", "serverchan", "wecom_bot", "bark"):
                with self.assertRaises(AppError) as ctx:
                    build_channel(
                        NotificationChannelConfig(
                            id="x", type=channel_type, config={}
                        ),
                        Path(tmp),
                    )
                self.assertEqual(ctx.exception.code, "CONFIG_INVALID")


if __name__ == "__main__":
    unittest.main()
