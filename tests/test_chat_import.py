from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.adapters.base import FetchOptions, SourceContext
from msgdesk.adapters.chat_import import ChatImportAdapter
from msgdesk.errors import AppError
from msgdesk.models import Cursor


def source(directory: Path, platform: str = "wechat") -> SourceContext:
    return SourceContext(
        id="import-source",
        type="chat_import",
        name="聊天记录导入",
        config={
            "directory": str(directory),
            "platform": platform,
            "file_globs": ["*.json", "*.jsonl", "*.csv"],
        },
    )


class ChatImportTests(unittest.TestCase):
    def _write_fixtures(self, directory: Path) -> None:
        (directory / "a.json").write_text(
            json.dumps(
                [
                    {
                        "time": 1750000000,
                        "conversation": "班级群",
                        "sender": "老师",
                        "content": "JSON 第一条",
                        "message_id": "j1",
                    },
                    {
                        "timestamp": 1750000000123,
                        "conversation": "班级群",
                        "sender": "老师",
                        "content": "JSON 第二条",
                        "msg_id": "j2",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (directory / "b.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "create_time": "2026-10-18 08:30:00",
                            "chatroom": "年级群",
                            "nickname": "班长",
                            "text": "JSONL 第三条",
                            "id": "l1",
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "date": "2026-10-18T09:00:00+08:00",
                            "group": "年级群",
                            "from": "同学",
                            "message": "",
                            "message_id": "l2",
                        },
                        ensure_ascii=False,
                    ),
                    "{bad json",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (directory / "c.csv").write_text(
            "Message_ID,Date,Sender,Chatroom,Content\n"
            "c1,2026-10-18 10:00:00,老师,班级群,CSV 内容\n",
            encoding="utf-8",
        )

    def test_json_jsonl_csv_import_and_incremental_append(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._write_fixtures(directory)
            adapter = ChatImportAdapter()
            first = adapter.fetch_incremental(
                source(directory), Cursor(), FetchOptions()
            )
            texts = {message.content_text for message in first.messages}
            self.assertEqual(
                texts,
                {"JSON 第一条", "JSON 第二条", "JSONL 第三条", "CSV 内容"},
            )
            self.assertTrue(any("无法解析为 JSON 对象" in warning for warning in first.warnings))
            first_by_text = {m.content_text: m for m in first.messages}
            self.assertEqual(first_by_text["JSON 第一条"].conversation_name, "班级群")
            self.assertEqual(first_by_text["JSON 第一条"].sender_name, "老师")
            self.assertTrue(first_by_text["JSON 第二条"].published_at.endswith("Z"))
            self.assertEqual(first_by_text["CSV 内容"].conversation_id, "班级群")
            self.assertEqual(first.next_cursor.cursor_type, "chat_import")

            second = adapter.fetch_incremental(
                source(directory), first.next_cursor, FetchOptions()
            )
            self.assertEqual(second.messages, [])

            with (directory / "b.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "time": 1750000500,
                            "conversation": "年级群",
                            "sender": "班长",
                            "content": "追加消息",
                            "message_id": "l3",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            third = adapter.fetch_incremental(
                source(directory), second.next_cursor, FetchOptions()
            )
            self.assertEqual([m.content_text for m in third.messages], ["追加消息"])

    def test_time_formats_and_fingerprint(self) -> None:
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._write_fixtures(directory)
            adapter = ChatImportAdapter()
            result = adapter.fetch_incremental(
                source(directory), Cursor(), FetchOptions()
            )
            for message in result.messages:
                self.assertIsNotNone(message.published_at)
                self.assertTrue(message.published_at.endswith("Z"))

            row = {
                "conversation": "群",
                "sender": "人",
                "content": "没有 id",
                "time": 1750000000,
            }
            path = directory / "a.json"
            first, _ = adapter._convert_row(row, path, "wechat", 1750000000.0)
            second, _ = adapter._convert_row(row, path, "wechat", 1750000000.0)
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(first.platform_message_id, second.platform_message_id)
            self.assertTrue(first.platform_message_id.startswith("import-"))

    def test_missing_directory_is_config_error(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "not-there"
            with self.assertRaises(AppError) as ctx:
                ChatImportAdapter().fetch_incremental(
                    source(missing), Cursor(), FetchOptions()
                )
            self.assertEqual(ctx.exception.code, "CONFIG_INVALID")
            self.assertFalse(missing.exists())

    def test_capabilities(self) -> None:
        caps = ChatImportAdapter().get_capabilities().to_dict()
        self.assertEqual(caps["adapter_type"], "chat_import")
        self.assertEqual(caps["content_level"], "complete")
        self.assertFalse(caps["requires_local_client"])
        self.assertTrue(caps["supports_incremental_cursor"])
        self.assertFalse(caps["supports_login"])


if __name__ == "__main__":
    unittest.main()
