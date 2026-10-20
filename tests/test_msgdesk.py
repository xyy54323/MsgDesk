from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk import __version__
from msgdesk.adapters.base import BaseAdapter, FetchOptions, FetchResponse, SourceContext
from msgdesk.adapters.web import WebAdapter
from msgdesk.api.server import ApiServer
from msgdesk.config import AppConfig
from msgdesk.db import Database
from msgdesk.errors import AppError
from msgdesk.models import Cursor, RawMessage
from msgdesk.repository import Repository
from msgdesk.runtime import Runtime
from msgdesk.services.digest import DigestService
from msgdesk.services.model import ModelInput, ModelProvider, create_model_provider
from msgdesk.services.processing import ProcessingService
from msgdesk.services.agent import AgentCollector
from msgdesk.services.sync import SyncService
from msgdesk.services.worker import Worker
from msgdesk.timeutil import iso_utc, local_date_of

FIXTURES = Path(__file__).parent / "fixtures"
PAGE_URL = "https://example.com/page"


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


class EmptyFetcher(FakeFetcher):
    def __init__(self):
        super().__init__({})


class FailingAdapter(BaseAdapter):
    adapter_type = "web"

    def fetch_incremental(self, source, cursor, options):
        raise AppError("NETWORK_TIMEOUT", "测试超时")


class FakeModel(ModelProvider):
    name = "fake"
    model_name = "fake-v1"

    def analyze(self, message: ModelInput, context: dict) -> dict:
        return {
            "category": "学校通知",
            "subcategory": None,
            "tags": ["开放日"],
            "summary_short": "实验室开放日安排",
            "summary_long": message.text[:500],
            "facts": [],
            "action_items": ["提前十分钟到场"],
            "deadline_at": None,
            "priority": "high",
            "urgency": "normal",
            "confidence": 0.9,
        }


def build_config(root: Path) -> AppConfig:
    config = AppConfig()
    config.base_dir = root
    config.system.data_dir = str(root / "data")
    config.system.digest_dir = str(root / "digests")
    config.ensure_dirs()
    return config


def build_repo(root: Path) -> Repository:
    db = Database(root / "data" / "messages.db")
    db.migrate()
    return Repository(db)


def create_web_source(
    repo: Repository,
    name: str = "网页",
    url: str = PAGE_URL,
    *,
    source_type: str = "web",
) -> dict:
    return repo.create_source({
        "type": source_type,
        "name": name,
        "adapter_type": source_type,
        "config": {"url": url, "mode": "page"},
        "interval_seconds": 600,
    })


class MigrationTests(unittest.TestCase):
    def test_migrations_are_idempotent_and_core_tables_exist(self):
        with TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            self.assertEqual(db.migrate(), [1, 2, 3, 4, 5, 6, 7])
            self.assertEqual(db.migrate(), [])
            self.assertEqual(db.schema_version(), 7)
            with db.connection() as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            for name in (
                "sources", "sync_cursors", "conversations", "messages",
                "message_processing", "jobs", "digests",
            ):
                self.assertIn(name, tables)


class AdapterTests(unittest.TestCase):
    def test_web_page_hash_cursor(self):
        fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
        adapter = WebAdapter(fetcher=fetcher)
        source = SourceContext("s1", "web", "网页", {"url": PAGE_URL, "mode": "page"})
        first = adapter.fetch_incremental(source, Cursor(), FetchOptions())
        self.assertEqual(len(first.messages), 1)
        second = adapter.fetch_incremental(source, first.next_cursor, FetchOptions())
        self.assertEqual(len(second.messages), 0)

    def test_web_page_sends_login_cookie(self):
        fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
        adapter = WebAdapter(fetcher=fetcher)
        source = SourceContext(
            "s2", "web", "内网",
            {"url": PAGE_URL, "mode": "page", "cookie": "SESSION=abc123"},
        )
        adapter.fetch_incremental(source, Cursor(), FetchOptions())
        self.assertEqual(fetcher.last_headers.get("Cookie"), "SESSION=abc123")


class SyncTests(unittest.TestCase):
    def test_sync_is_idempotent_and_queues_processing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_web_source(repo)
            fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
            service = SyncService(
                config, repo, adapter_factory=lambda _: WebAdapter(fetcher=fetcher)
            )
            first = service.sync_source(source["id"])
            second = service.sync_source(source["id"])
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["inserted"], 0)
            self.assertEqual(len(repo.list_messages(limit=100)), 1)
            refreshed = repo.get_source(source["id"])
            self.assertIsNotNone(refreshed["last_success_at"])
            jobs = repo.list_jobs(50)
            self.assertEqual(sum(1 for job in jobs if job["type"] == "process_message"), 1)

    def test_cross_source_content_is_merged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            first = create_web_source(repo, "源 A")
            second = create_web_source(repo, "源 B")
            fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
            service = SyncService(
                config, repo, adapter_factory=lambda _: WebAdapter(fetcher=fetcher)
            )
            service.sync_source(first["id"])
            result = service.sync_source(second["id"])
            self.assertEqual(result["merged"], 1)
            self.assertEqual(len(repo.list_messages(limit=100)), 1)

    def test_unprocessed_messages_use_source_default_category(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_web_source(repo)
            repo.update_source(source["id"], {"default_category": "研究生通知"})
            fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
            SyncService(
                config, repo, adapter_factory=lambda _: WebAdapter(fetcher=fetcher)
            ).sync_source(source["id"])
            message = repo.list_messages(limit=1)[0]
            self.assertEqual(message["category"], "研究生通知")


class ProcessingTests(unittest.TestCase):
    def _seed(self, root: Path, config: AppConfig, repo: Repository) -> dict:
        source = create_web_source(repo)
        fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
        SyncService(
            config, repo, adapter_factory=lambda _: WebAdapter(fetcher=fetcher)
        ).sync_source(source["id"])
        return source

    def test_model_processing_and_source_category(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = self._seed(root, config, repo)
            repo.update_source(source["id"], {"default_category": "学校通知"})
            message = repo.list_messages(limit=1)[0]
            service = ProcessingService(config, repo, FakeModel())
            result = service.process(message["id"])
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["category"], "学校通知")
            saved = repo.get_message(message["id"])
            self.assertEqual(saved["processing_status"], "processed")
            self.assertTrue(saved["processing"]["summary_short"])

    def test_unconfigured_model_marks_message_failed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            self._seed(root, config, repo)
            message = repo.list_messages(limit=1)[0]
            model = create_model_provider(config.model, config.system.timezone)
            result = ProcessingService(config, repo, model).process(message["id"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "CONFIG_INVALID")
            saved = repo.get_message(message["id"])
            self.assertEqual(saved["processing_status"], "failed")


class DigestTests(unittest.TestCase):
    def test_global_range_generates_empty_days(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.date_range.start = "2026-09-01"
            config.date_range.end = "2026-09-03"
            repo = build_repo(root)
            count = DigestService(config, repo).generate_missing()
            self.assertEqual(count, 3)
            self.assertEqual(
                [item["date"] for item in repo.list_digests(10)],
                ["2026-09-03", "2026-09-02", "2026-09-01"],
            )

    def test_digest_is_byte_idempotent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_web_source(repo)
            fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
            SyncService(
                config, repo, adapter_factory=lambda _: WebAdapter(fetcher=fetcher)
            ).sync_source(source["id"])
            processing = ProcessingService(config, repo, FakeModel())
            for message in repo.list_messages(limit=50):
                processing.process(message["id"])
            digest = DigestService(config, repo)
            message = repo.list_messages(limit=1)[0]
            date_text = local_date_of(message["published_at"], config.system.timezone)
            first = digest.generate(date_text)
            first_content = Path(first["file_path"]).read_text(encoding="utf-8")
            second = digest.generate(date_text)
            second_content = Path(second["file_path"]).read_text(encoding="utf-8")
            self.assertEqual(first_content, second_content)
            self.assertFalse(second["changed"])
            self.assertIn("实验室开放日安排", first_content)
            self.assertIn("今日待办", first_content)


class AgentRangeTests(unittest.TestCase):
    def test_agent_keeps_items_inside_global_range(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.date_range.start = "2026-09-01"
            config.date_range.end = "2026-10-20"
            collector = AgentCollector(config)
            payload = json.dumps({
                "items": [
                    {
                        "title": "范围内旧通知",
                        "url": "https://example.com/old",
                        "published_at": "2026-10-18",
                        "content_md": "旧通知正文",
                    },
                    {
                        "title": "范围外通知",
                        "url": "https://example.com/outside",
                        "published_at": "2026-08-01",
                        "content_md": "范围外正文",
                    },
                    {
                        "title": "今天通知",
                        "url": "https://example.com/today",
                        "published_at": "2026-10-20",
                        "content_md": "今天正文",
                    },
                ]
            }, ensure_ascii=False)
            source = SourceContext(
                "s1", "ai_web", "网站", {"url": "https://example.com/"}
            )
            result = collector._finish(
                payload, source, "https://example.com/", {}, []
            )
            self.assertEqual(
                [message.conversation_name for message in result.messages],
                ["范围内旧通知", "今天通知"],
            )


class AttachmentTests(unittest.TestCase):
    def test_attachment_download_keeps_file_type(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"docx-content"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = build_config(root)
                service = SyncService(config, build_repo(root))
                raw = RawMessage(
                    content_text="[文件: 通知.docx]",
                    published_at=iso_utc(),
                    attachments=[{
                        "type": "file",
                        "name": "通知.docx",
                        "url": f"http://127.0.0.1:{server.server_address[1]}/file",
                    }],
                )
                saved = service._download_attachments(raw, "source-123", [])
                self.assertEqual(saved[0][0], "通知.docx")
                self.assertTrue(saved[0][1].endswith(".docx"))
                stored = root / "data" / saved[0][1].lstrip("/")
                self.assertEqual(stored.read_bytes(), b"docx-content")
        finally:
            server.shutdown()
            server.server_close()


class JobTests(unittest.TestCase):
    def test_interrupted_running_job_is_requeued(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = build_repo(root)
            source = create_web_source(repo, "恢复测试")
            job_id = repo.enqueue_job("sync_source", source_id=source["id"])
            self.assertIsNotNone(repo.claim_next_job())
            self.assertEqual(repo.get_job(job_id)["status"], "running")
            self.assertEqual(repo.recover_interrupted_jobs(), 1)
            recovered = repo.get_job(job_id)
            self.assertEqual(recovered["status"], "retry_waiting")
            self.assertEqual(recovered["error_code"], "INTERRUPTED")

    def test_retryable_job_enters_retry_waiting(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_web_source(repo, "失败源")
            sync_service = SyncService(
                config, repo, adapter_factory=lambda _: FailingAdapter()
            )
            worker = Worker(
                repo,
                sync_service,
                ProcessingService(config, repo, FakeModel()),
                DigestService(config, repo),
            )
            job_id = repo.enqueue_job("sync_source", source_id=source["id"])
            self.assertTrue(worker.run_once())
            job = repo.get_job(job_id)
            self.assertEqual(job["status"], "retry_waiting")
            self.assertEqual(job["error_code"], "NETWORK_TIMEOUT")

    def test_worker_processes_pending_messages(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = create_web_source(repo)
            fetcher = FakeFetcher({PAGE_URL: (FIXTURES / "page.html").read_bytes()})
            sync_service = SyncService(
                config, repo, adapter_factory=lambda _: WebAdapter(fetcher=fetcher)
            )
            worker = Worker(
                repo,
                sync_service,
                ProcessingService(config, repo, FakeModel()),
                DigestService(config, repo),
            )
            sync_service.sync_source(source["id"])
            processed = 0
            while worker.run_once() and processed < 20:
                processed += 1
            messages = repo.list_messages(limit=50)
            self.assertTrue(messages)
            self.assertTrue(all(m["processing_status"] == "processed" for m in messages))


class ApiTests(unittest.TestCase):
    def test_health_sources_static_and_status(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp))
            runtime = Runtime(config)
            server = ApiServer(("127.0.0.1", 0), runtime)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=5) as response:
                    health = json.loads(response.read())
                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["version"], __version__)
                payload = json.dumps({
                    "type": "ai_web",
                    "name": "API 来源",
                    "config": {"url": PAGE_URL},
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
                    page = response.read()
                self.assertIn(b"MSGDESK", page)
                self.assertIn(b"/app.js", page)
                with urllib.request.urlopen(base + "/app.js", timeout=5) as response:
                    self.assertIn(b"const VIEWS", response.read())
                with urllib.request.urlopen(base + "/app.css", timeout=5) as response:
                    self.assertIn(b"--paper", response.read())
            finally:
                server.shutdown()
                server.server_close()
                runtime.stop()


if __name__ == "__main__":
    unittest.main()
