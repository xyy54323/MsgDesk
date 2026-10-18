from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.adapters.base import BaseAdapter, FetchResponse
from msgdesk.adapters.rss import RSSAdapter
from msgdesk.adapters.web import WebAdapter
from msgdesk.api.server import ApiServer
from msgdesk.config import (
    AppConfig,
    NotificationChannelConfig,
    QuietHours,
)
from msgdesk.db import Database
from msgdesk.errors import AppError
from msgdesk.models import Cursor
from msgdesk.repository import Repository
from msgdesk.runtime import Runtime
from msgdesk.services.digest import DigestService
from msgdesk.services.model import create_model_provider
from msgdesk.services.notifications import NotificationService
from msgdesk.services.processing import ProcessingService
from msgdesk.services.rules import RuleEngine
from msgdesk.services.sync import SyncService
from msgdesk.services.worker import Worker
from msgdesk.timeutil import local_date_of, utc_now

FIXTURES = Path(__file__).parent / "fixtures"


class FakeFetcher:
    def __init__(self, mapping: dict[str, bytes]):
        self.mapping = mapping
        self.calls: list[str] = []
        self.last_headers: dict[str, str] = {}

    def fetch(self, url: str, *, etag=None, last_modified=None, timeout=20.0, headers=None):
        self.calls.append(url)
        self.last_headers = dict(headers or {})
        if url not in self.mapping:
            raise AppError("NETWORK_UNAVAILABLE", f"no fixture for {url}")
        return FetchResponse(200, self.mapping[url], {}, url)


class FailingAdapter(BaseAdapter):
    adapter_type = "rss"

    def fetch_incremental(self, source, cursor, options):
        raise AppError("NETWORK_TIMEOUT", "测试超时")


def build_config(root: Path) -> AppConfig:
    config = AppConfig()
    config.base_dir = root
    config.system.data_dir = str(root / "data")
    config.system.digest_dir = str(root / "digests")
    config.system.require_auth = False
    config.scheduling.quiet_hours = QuietHours(start="03:00", end="03:01")
    config.notifications.min_priority = "normal"
    config.notifications.channels = [
        NotificationChannelConfig(id="mock", type="mock", enabled=True,
                                  config={"notify_digest": True})
    ]
    config.ensure_dirs()
    return config


def build_repo(root: Path) -> Repository:
    db = Database(root / "data" / "messages.db")
    db.migrate()
    return Repository(db)


def create_rss_source(repo: Repository, name: str, url: str) -> dict:
    return repo.create_source({
        "type": "rss",
        "name": name,
        "adapter_type": "rss",
        "config": {"url": url, "max_items": 20},
        "interval_seconds": 600,
    })


class MigrationTests(unittest.TestCase):
    def test_migrations_are_idempotent_and_tables_exist(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            self.assertEqual(db.migrate(), [1, 2, 3, 4, 5, 6])
            self.assertEqual(db.migrate(), [])
            self.assertEqual(db.schema_version(), 6)
            with db.connection() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            for name in (
                "sources", "accounts", "sync_cursors", "conversations", "messages",
                "message_sources", "message_processing", "jobs", "digests",
                "notification_records", "rules", "audit_logs", "model_calls",
            ):
                self.assertIn(name, tables)


class AdapterTests(unittest.TestCase):
    def test_rss_and_atom_parsing(self):
        fetcher = FakeFetcher({
            "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes(),
            "https://example.com/atom.xml": (FIXTURES / "atom.xml").read_bytes(),
        })
        rss = RSSAdapter(fetcher=fetcher)
        from msgdesk.adapters.base import FetchOptions, SourceContext

        result = rss.fetch_incremental(
            SourceContext("s1", "rss", "测试", {"url": "https://example.com/feed.xml"}),
            Cursor(),
            FetchOptions(),
        )
        self.assertEqual(len(result.messages), 3)
        self.assertEqual(result.messages[0].platform_message_id, "fixture-exam-1")
        self.assertIsNotNone(result.next_cursor.last_message_time)
        atom = rss.fetch_incremental(
            SourceContext("s2", "rss", "Atom", {"url": "https://example.com/atom.xml"}),
            Cursor(),
            FetchOptions(),
        )
        self.assertEqual(len(atom.messages), 1)
        self.assertIn("增量同步", atom.messages[0].content_text)

    def test_web_page_hash_cursor(self):
        page_url = "https://example.com/page"
        fetcher = FakeFetcher({page_url: (FIXTURES / "page.html").read_bytes()})
        adapter = WebAdapter(fetcher=fetcher)
        from msgdesk.adapters.base import FetchOptions, SourceContext

        source = SourceContext("s1", "web", "网页", {"url": page_url, "mode": "page"})
        first = adapter.fetch_incremental(source, Cursor(), FetchOptions())
        self.assertEqual(len(first.messages), 1)
        second = adapter.fetch_incremental(source, first.next_cursor, FetchOptions())
        self.assertEqual(len(second.messages), 0)

    def test_web_page_sends_login_cookie(self):
        page_url = "https://example.com/private"
        fetcher = FakeFetcher({page_url: (FIXTURES / "page.html").read_bytes()})
        adapter = WebAdapter(fetcher=fetcher)
        from msgdesk.adapters.base import FetchOptions, SourceContext

        source = SourceContext(
            "s2", "web", "内网", {
                "url": page_url, "mode": "page", "cookie": "SESSION=abc123"
            }
        )
        adapter.fetch_incremental(source, Cursor(), FetchOptions())
        self.assertEqual(fetcher.last_headers.get("Cookie"), "SESSION=abc123")

    def test_chat_placeholder_has_no_send_methods(self):
        from msgdesk.adapters import create_adapter

        adapter = create_adapter("qq")
        self.assertFalse(any(
            hasattr(adapter, name) for name in ("send", "reply", "send_message", "add_friend")
        ))
        with self.assertRaises(AppError) as ctx:
            from msgdesk.adapters.base import FetchOptions, SourceContext

            adapter.fetch_incremental(
                SourceContext("s", "qq", "QQ", {}), Cursor(), FetchOptions()
            )
        self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")


class SyncTests(unittest.TestCase):
    def test_sync_is_idempotent_and_updates_source(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_rss_source(repo, "测试源", "https://example.com/feed.xml")
            fetcher = FakeFetcher({
                "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes()
            })
            service = SyncService(
                config, repo, adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)
            )
            first = service.sync_source(source["id"])
            self.assertEqual(first["inserted"], 3)
            second = service.sync_source(source["id"])
            self.assertEqual(second["inserted"], 0)
            self.assertEqual(len(repo.list_messages(limit=100)), 3)
            refreshed = repo.get_source(source["id"])
            self.assertIsNotNone(refreshed["last_success_at"])
            jobs = repo.list_jobs(50)
            self.assertEqual(sum(1 for j in jobs if j["type"] == "process_message"), 3)

    def test_cross_source_content_is_merged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            first = create_rss_source(repo, "源 A", "https://example.com/feed.xml")
            second = create_rss_source(repo, "源 B", "https://example.com/feed2.xml")
            fetcher = FakeFetcher({
                "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes(),
                "https://example.com/feed2.xml": (FIXTURES / "feed2.xml").read_bytes(),
            })
            service = SyncService(
                config, repo, adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)
            )
            service.sync_source(first["id"])
            result = service.sync_source(second["id"])
            self.assertEqual(result["merged"], 1)
            self.assertEqual(len(repo.list_messages(limit=100)), 3)
            tech = next(
                m for m in repo.list_messages(limit=100) if "本地检索" in (m["content_text"] or "")
            )
            detail = repo.get_message(tech["id"])
            self.assertGreaterEqual(len(detail["sources"]), 2)

    def test_web_source_change_creates_new_message(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            url = "https://example.com/page"
            body_holder = {"value": (FIXTURES / "page.html").read_bytes()}

            class MutableFetcher(FakeFetcher):
                def fetch(self, url, **kwargs):
                    return FetchResponse(200, body_holder["value"], {}, url)

            source = repo.create_source({
                "type": "web", "name": "网页", "adapter_type": "web",
                "config": {"url": url, "mode": "page"}, "interval_seconds": 600,
            })
            service = SyncService(
                config, repo, adapter_factory=lambda _: WebAdapter(fetcher=MutableFetcher({}))
            )
            service.sync_source(source["id"])
            service.sync_source(source["id"])
            self.assertEqual(len(repo.list_messages(limit=100)), 1)
            body_holder["value"] = body_holder["value"].replace(
                "三楼实验室".encode(), "五楼会议室".encode()
            )
            service.sync_source(source["id"])
            self.assertEqual(len(repo.list_messages(limit=100)), 2)


class ProcessingTests(unittest.TestCase):
    def _seed(self, root: Path, config: AppConfig, repo: Repository) -> dict:
        source = create_rss_source(repo, "测试源", "https://example.com/feed.xml")
        fetcher = FakeFetcher({
            "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes()
        })
        SyncService(config, repo, adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)).sync_source(
            source["id"]
        )
        return source

    def test_rule_processing_and_priority(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            self._seed(root, config, repo)
            repo.create_rule({
                "name": "学校通知",
                "priority": 20,
                "condition": {"keywords": ["报名"]},
                "action": {"category": "学校通知", "priority": "urgent", "tags": ["报名"]},
            })
            model = create_model_provider(config.model, config.system.timezone)
            service = ProcessingService(config, repo, model)
            message = next(
                m for m in repo.list_messages(limit=100) if "报名" in (m["content_text"] or "")
            )
            result = service.process(message["id"])
            self.assertEqual(result["category"], "学校通知")
            self.assertEqual(result["priority"], "urgent")
            saved = repo.get_message(message["id"])
            self.assertIn("报名", saved["processing"]["tags"])
            self.assertIsNotNone(saved["processing"]["deadline_at"])

    def test_model_failure_falls_back_and_keeps_message(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.model.provider = "openai_compatible"
            config.model.base_url = "http://127.0.0.1:9/v1"
            config.model.model = "not-configured"
            config.model.api_key_env = "MSGDESK_TEST_MISSING_KEY"
            repo = build_repo(root)
            self._seed(root, config, repo)
            model = create_model_provider(config.model, config.system.timezone)
            service = ProcessingService(config, repo, model)
            message = repo.list_messages(limit=1)[0]
            result = service.process(message["id"])
            self.assertEqual(result["status"], "fallback")
            self.assertEqual(result["error_code"], "AUTH_REQUIRED")
            saved = repo.get_message(message["id"])
            self.assertEqual(saved["processing_status"], "processed")
            self.assertTrue(saved["processing"]["summary_short"])
            self.assertIn("报名", saved["content_text"])

    def test_manual_override_creates_new_current_version(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            self._seed(root, config, repo)
            message = repo.list_messages(limit=1)[0]
            model = create_model_provider(config.model, config.system.timezone)
            ProcessingService(config, repo, model).process(message["id"])
            repo.override_processing(message["id"], {"category": "项目", "priority": "low"})
            saved = repo.get_message(message["id"])
            self.assertEqual(saved["processing"]["category"], "项目")
            self.assertEqual(saved["processing"]["priority"], "low")
            with repo.db.connection() as conn:
                current_count = conn.execute(
                    "SELECT COUNT(*) FROM message_processing WHERE message_id = ? "
                    "AND is_current = 1",
                    (message["id"],),
                ).fetchone()[0]
            self.assertEqual(current_count, 1)

    def test_negated_keyword_does_not_trigger_rule(self):
        engine = RuleEngine([{
            "id": "r1",
            "name": "报名规则",
            "priority": 10,
            "enabled": 1,
            "scope_type": "global",
            "scope_id": None,
            "condition_json": json.dumps({"keywords": ["报名"]}),
            "action_json": json.dumps({"priority": "high"}),
        }])
        result = engine.apply(source_id="s1", content_type="text",
                              text="本周社区活动可以参加，无需报名。")
        self.assertEqual(result["matched"], [])
        positive = engine.apply(source_id="s1", content_type="text",
                                text="请于明天前完成报名。")
        self.assertEqual(positive["matched"], ["报名规则"])


class DigestTests(unittest.TestCase):
    def test_digest_is_byte_idempotent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_rss_source(repo, "测试源", "https://example.com/feed.xml")
            fetcher = FakeFetcher({
                "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes()
            })
            SyncService(config, repo, adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)).sync_source(
                source["id"]
            )
            model = create_model_provider(config.model, config.system.timezone)
            processing = ProcessingService(config, repo, model)
            for message in repo.list_messages(limit=50):
                processing.process(message["id"])
            digest = DigestService(config, repo)
            date_text = local_date_of(repo.list_messages(limit=1)[0]["published_at"],
                                      config.system.timezone)
            first = digest.generate(date_text)
            content_first = Path(first["file_path"]).read_text(encoding="utf-8")
            second = digest.generate(date_text)
            content_second = Path(second["file_path"]).read_text(encoding="utf-8")
            self.assertEqual(content_first, content_second)
            self.assertFalse(second["changed"])
            self.assertGreaterEqual(content_first.count("教务处关于期中考试报名的通知"), 1)
            self.assertIn("今日待办", content_first)
            self.assertIn("来源索引", content_first)
            self.assertNotIn("。，截止", content_first)


class NotificationTests(unittest.TestCase):
    def test_notification_is_deduplicated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_rss_source(repo, "测试源", "https://example.com/feed.xml")
            fetcher = FakeFetcher({
                "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes()
            })
            SyncService(config, repo, adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)).sync_source(
                source["id"]
            )
            message = repo.list_messages(limit=1)[0]
            model = create_model_provider(config.model, config.system.timezone)
            ProcessingService(config, repo, model).process(message["id"])
            service = NotificationService(config, repo)
            first = service.notify_message(message["id"], "mock")
            second = service.notify_message(message["id"], "mock")
            self.assertEqual(first["status"], "sent")
            self.assertEqual(second["status"], "suppressed")
            self.assertEqual(second["reason"], "duplicate")
            self.assertEqual(repo.stats()["notifications_sent"], 1)

    def test_quiet_hours_suppress_non_urgent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.scheduling.quiet_hours = QuietHours(start="00:00", end="23:59",
                                                        allow_urgent=False)
            repo = build_repo(root)
            source = create_rss_source(repo, "测试源", "https://example.com/feed.xml")
            fetcher = FakeFetcher({
                "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes()
            })
            SyncService(config, repo, adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)).sync_source(
                source["id"]
            )
            message = repo.list_messages(limit=1)[0]
            model = create_model_provider(config.model, config.system.timezone)
            ProcessingService(config, repo, model).process(message["id"])
            result = NotificationService(config, repo).notify_message(message["id"], "mock")
            self.assertEqual(result["status"], "suppressed")
            self.assertEqual(result["reason"], "quiet_hours")

    def test_each_channel_gets_its_own_notification_job(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.notifications.channels = [
                NotificationChannelConfig(id="mock-a", type="mock", enabled=True),
                NotificationChannelConfig(id="mock-b", type="mock", enabled=True),
            ]
            repo = build_repo(root)
            source = create_rss_source(repo, "测试源", "https://example.com/feed.xml")
            fetcher = FakeFetcher({
                "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes()
            })
            SyncService(config, repo,
                        adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)).sync_source(
                source["id"]
            )
            message = repo.list_messages(limit=1)[0]
            model = create_model_provider(config.model, config.system.timezone)
            repo.create_rule({
                "name": "提醒所有", "priority": 1,
                "condition": {}, "action": {"notify": True},
            })
            ProcessingService(config, repo, model).process(message["id"])
            jobs = [j for j in repo.list_jobs(100) if j["type"] == "send_notification"]
            channels = {json.loads(j["payload_json"])["channel"] for j in jobs}
            self.assertEqual(channels, {"mock-a", "mock-b"})


class JobTests(unittest.TestCase):
    def test_retryable_job_enters_retry_waiting(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_rss_source(repo, "失败源", "https://example.com/feed.xml")
            sync_service = SyncService(config, repo, adapter_factory=lambda _: FailingAdapter())
            model = create_model_provider(config.model, config.system.timezone)
            worker = Worker(
                repo,
                sync_service,
                ProcessingService(config, repo, model),
                DigestService(config, repo),
                NotificationService(config, repo),
            )
            job_id = repo.enqueue_job("sync_source", source_id=source["id"])
            self.assertTrue(worker.run_once())
            job = repo.get_job(job_id)
            self.assertEqual(job["status"], "retry_waiting")
            self.assertEqual(job["error_code"], "NETWORK_TIMEOUT")
            self.assertEqual(job["attempts"], 1)

    def test_worker_processes_pending_messages(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_rss_source(repo, "测试源", "https://example.com/feed.xml")
            fetcher = FakeFetcher({
                "https://example.com/feed.xml": (FIXTURES / "feed.xml").read_bytes()
            })
            model = create_model_provider(config.model, config.system.timezone)
            sync_service = SyncService(
                config, repo, adapter_factory=lambda _: RSSAdapter(fetcher=fetcher)
            )
            digest_service = DigestService(config, repo)
            notification = NotificationService(config, repo)
            worker = Worker(repo, sync_service, ProcessingService(config, repo, model),
                            digest_service, notification)
            sync_service.sync_source(source["id"])
            processed = 0
            while worker.run_once() and processed < 20:
                processed += 1
            messages = repo.list_messages(limit=50)
            self.assertTrue(all(m["processing_status"] == "processed" for m in messages))
            self.assertGreaterEqual(processed, 4)


class ApiTests(unittest.TestCase):
    def test_health_sources_and_status(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            runtime = Runtime(config)
            server = ApiServer(("127.0.0.1", 0), runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
                    health = json.loads(response.read())
                self.assertEqual(health["status"], "ok")
                payload = json.dumps({
                    "type": "rss", "name": "API 来源",
                    "config": {"url": "https://example.com/feed.xml"},
                }).encode()
                request = urllib.request.Request(
                    base + "/api/sources", data=payload, method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    created = json.loads(response.read())
                self.assertEqual(created["name"], "API 来源")
                with urllib.request.urlopen(base + "/api/status", timeout=5) as response:
                    status = json.loads(response.read())
                self.assertEqual(status["stats"]["sources_enabled"], 1)
                with urllib.request.urlopen(base + "/", timeout=5) as response:
                    self.assertIn(b"MsgDesk", response.read())
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()


if __name__ == "__main__":
    unittest.main()

