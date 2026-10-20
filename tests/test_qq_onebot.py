from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from msgdesk.adapters.qq_onebot import QQOneBotAdapter
from msgdesk.adapters.base import FetchOptions, SourceContext
from msgdesk.errors import AppError
from msgdesk.models import Cursor
from msgdesk.timeutil import iso_utc, local_zone, utc_now


class _OneBotHandler(BaseHTTPRequestHandler):
    actions: list[tuple[str, dict[str, Any]]] = []
    messages: list[dict[str, Any]] = []
    unauthorized = False

    def do_POST(self) -> None:
        action = self.path.strip("/")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        type(self).actions.append((action, body))
        if type(self).unauthorized:
            self._send(401, {"status": "failed", "retcode": 401})
            return
        if action == "get_status":
            data = {"online": True, "good": True, "app_name": "NapCat", "app_version": "4.8.0"}
        elif action == "get_login_info":
            data = {"user_id": 123456789, "nickname": "测试帐号"}
        elif action in ("get_group_msg_history", "get_friend_msg_history"):
            data = {"messages": type(self).messages}
        elif action == "get_group_file_url":
            data = {"url": "https://example.com/notice.pdf"}
        else:
            data = {}
        self._send(200, {"status": "ok", "retcode": 0, "data": data})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return None


def source(base_url: str) -> SourceContext:
    return SourceContext(
        id="s1",
        type="qq_onebot",
        name="QQ 测试群",
        config={
            "base_url": base_url,
            "access_token": "secret-token",
            "selected_conversations": ["123456"],
            "count_per_poll": 100,
            "timeout_seconds": 3,
        },
    )


def message(message_id: int, seconds: int, segments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "group_id": 123456,
        "user_id": 98765,
        "message_type": "group",
        "time": seconds,
        "sender": {"card": "班长", "nickname": "小明"},
        "message": segments,
        "raw_message": "raw",
    }


class QQOneBotTests(unittest.TestCase):
    def setUp(self) -> None:
        _OneBotHandler.actions = []
        _OneBotHandler.messages = []
        _OneBotHandler.unauthorized = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _OneBotHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_capabilities(self) -> None:
        caps = QQOneBotAdapter().get_capabilities().to_dict()
        self.assertEqual(caps["adapter_type"], "qq_onebot")
        self.assertEqual(caps["adapter_version"], "0.2.0")
        self.assertTrue(caps["supports_login"])
        self.assertTrue(caps["supports_group"])
        self.assertTrue(caps["supports_private"])
        self.assertTrue(caps["supports_attachments"])
        self.assertTrue(caps["supports_stable_message_id"])
        self.assertTrue(caps["supports_incremental_cursor"])
        self.assertEqual(caps["content_level"], "complete")
        self.assertTrue(caps["requires_local_client"])

    def test_login_returns_masked_account(self) -> None:
        result = QQOneBotAdapter().login(source(self.base))
        self.assertEqual(result["nickname"], "测试帐号")
        self.assertEqual(result["account"], "123***89")
        self.assertNotIn("123456789", result["account"])

    def test_connection_refused_maps_to_client_not_running(self) -> None:
        closed = ThreadingHTTPServer(("127.0.0.1", 0), _OneBotHandler)
        port = closed.server_address[1]
        closed.server_close()
        adapter = QQOneBotAdapter()
        status = adapter.get_status(source(f"http://127.0.0.1:{port}"))
        self.assertEqual(status.status, "CLIENT_NOT_RUNNING")

    def test_unauthorized_maps_to_auth_required(self) -> None:
        _OneBotHandler.unauthorized = True
        status = QQOneBotAdapter().get_status(source(self.base))
        self.assertEqual(status.status, "AUTH_REQUIRED")

    def test_first_and_second_sync_use_cursor(self) -> None:
        now = int(utc_now().timestamp())
        _OneBotHandler.messages = [
            message(101, now - 120, [{"type": "text", "data": {"text": "第一条"}}]),
            message(102, now - 60, [{"type": "text", "data": {"text": "第二条"}}]),
        ]
        adapter = QQOneBotAdapter()
        first = adapter.fetch_incremental(source(self.base), Cursor(), FetchOptions())
        self.assertEqual(len(first.messages), 2)
        self.assertEqual(first.next_cursor.cursor_type, "onebot")
        self.assertEqual(first.next_cursor.cursor_value, "102")
        self.assertEqual(first.next_cursor.last_platform_message_id, "102")
        self.assertEqual(first.messages[0].content_text, "第一条")

        second = adapter.fetch_incremental(
            source(self.base), first.next_cursor, FetchOptions()
        )
        self.assertEqual(len(second.messages), 0)
        history_calls = [
            body for action, body in _OneBotHandler.actions
            if action == "get_group_msg_history"
        ]
        self.assertEqual(history_calls[-1]["message_seq"], 102)
        self.assertEqual(history_calls[-1]["group_id"], "123456")
        self.assertEqual(history_calls[-1]["reverseOrder"], False)

    def test_segments_are_converted(self) -> None:
        now = int(time.time())
        _OneBotHandler.messages = [
            message(
                300,
                now,
                [
                    {"type": "text", "data": {"text": "你好 "}},
                    {"type": "image", "data": {"url": "https://example.com/a.png"}},
                    {"type": "at", "data": {"name": "小明"}},
                    {"type": "face", "data": {"id": "1"}},
                    {"type": "reply", "data": {"id": "1"}},
                ],
            )
        ]
        result = QQOneBotAdapter().fetch_incremental(
            source(self.base), Cursor(), FetchOptions()
        )
        self.assertEqual(len(result.messages), 1)
        converted = result.messages[0]
        self.assertIn("[图片]", converted.content_text)
        self.assertIn("@小明", converted.content_text)
        self.assertIn("[face]", converted.content_text)
        self.assertNotIn("reply", converted.content_text)
        self.assertEqual(converted.attachments[0]["url"], "https://example.com/a.png")
        self.assertEqual(converted.sender_name, "班长")
        self.assertEqual(converted.conversation_kind, "group")
        self.assertTrue(converted.published_at.endswith("Z"))

    def test_fetch_range_keeps_messages_inside_window(self) -> None:
        now = int(time.time())
        old = now - 40 * 86400
        _OneBotHandler.messages = [
            message(401, old, [{"type": "text", "data": {"text": "旧消息"}}]),
            message(402, now, [{"type": "text", "data": {"text": "新消息"}}]),
        ]
        zone = local_zone("Asia/Shanghai")
        start = datetime.fromtimestamp(old - 3600, zone)
        end = datetime.fromtimestamp(old + 3600, zone)
        result = QQOneBotAdapter().fetch_range(source(self.base), start, end)
        self.assertEqual([m.content_text for m in result.messages], ["旧消息"])

    def test_file_segment_resolves_download_url(self) -> None:
        now = int(time.time())
        _OneBotHandler.messages = [
            message(
                501,
                now,
                [
                    {
                        "type": "file",
                        "data": {
                            "file": "通知.pdf",
                            "file_id": "file-501",
                        },
                    }
                ],
            )
        ]
        result = QQOneBotAdapter().fetch_incremental(
            source(self.base), Cursor(), FetchOptions()
        )
        self.assertEqual(len(result.messages), 1)
        self.assertIn("[文件: 通知.pdf]", result.messages[0].content_text)
        self.assertEqual(
            result.messages[0].attachments[0]["url"],
            "https://example.com/notice.pdf",
        )

    def test_no_write_actions_are_called(self) -> None:
        _OneBotHandler.messages = []
        adapter = QQOneBotAdapter()
        adapter.get_status(source(self.base))
        adapter.login(source(self.base))
        adapter.fetch_incremental(source(self.base), Cursor(), FetchOptions())
        for name in ("send", "reply", "send_message", "add_friend", "recall"):
            self.assertFalse(hasattr(adapter, name))
        self.assertTrue(_OneBotHandler.actions)
        for action, _ in _OneBotHandler.actions:
            self.assertFalse(action.startswith("send"), action)
            self.assertFalse(action.startswith("delete"), action)


if __name__ == "__main__":
    unittest.main()
