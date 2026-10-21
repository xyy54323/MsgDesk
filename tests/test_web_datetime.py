from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.adapters.base import Cursor, FetchOptions, SourceContext
from msgdesk.config import AppConfig
from msgdesk.db import Database
from msgdesk.models import FetchResult, RawMessage, ValidationResult
from msgdesk.repository import Repository
from msgdesk.services.sync import SyncService
from msgdesk.timeutil import parse_source_datetime


class OneMessageAdapter:
    adapter_type = "web"

    def __init__(self, message: RawMessage) -> None:
        self.message = message

    def validate_config(self, config):
        return ValidationResult(True)

    def fetch_incremental(self, source, cursor, options):
        return FetchResult(messages=[self.message], next_cursor=Cursor())

    def close(self):
        return None


def build_config(root: Path) -> AppConfig:
    config = AppConfig()
    config.base_dir = root
    config.system.data_dir = str(root / "data")
    config.system.digest_dir = str(root / "digests")
    config.ensure_dirs()
    return config


class SourcePublishedTimeTests(unittest.TestCase):
    def test_chinese_article_time_is_interpreted_in_local_zone(self):
        parsed = parse_source_datetime(
            "时间：2026年10月21日 09:10:48", "Asia/Shanghai"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(), "2026-10-21T01:10:48+00:00")

    def test_sync_persists_date_only_time_flag(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            database = Database(root / "data" / "messages.db")
            database.migrate()
            repo = Repository(database)
            source = repo.create_source(
                {
                    "type": "web",
                    "name": "时间测试",
                    "adapter_type": "web",
                    "config": {"url": "https://example.test"},
                }
            )
            message = RawMessage(
                platform_message_id="time-only-1",
                conversation_id="https://example.test",
                conversation_name="时间测试",
                content_text="只有日期的消息",
                published_at="2026-10-21T00:00:00Z",
                published_time_known=False,
                source_url="https://example.test/article",
            )
            service = SyncService(
                config,
                repo,
                adapter_factory=lambda _: OneMessageAdapter(message),
            )
            service.sync_source(source["id"])
            conn = sqlite3.connect(database.path)
            try:
                row = conn.execute(
                    "select published_time_known from messages where platform_message_id = ?",
                    ("time-only-1",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row[0], 0)


if __name__ == "__main__":
    unittest.main()
