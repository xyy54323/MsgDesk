import json
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.models import FetchResult, RawMessage, ValidationResult
from msgdesk.services.conversation_digest import ConversationDigestService
from msgdesk.services.digest import DigestService
from msgdesk.services.sync import SyncService
from msgdesk.services.worker import Worker
from tests.test_msgdesk import build_config, build_repo


class FakeQQAdapter:
    def __init__(self, messages):
        self.messages = messages

    def validate_config(self, config):
        return ValidationResult(ok=True)

    def fetch_incremental(self, source, cursor, options):
        return FetchResult(messages=self.messages)

    def close(self):
        return None


def qq_source(repo):
    return repo.create_source({
        "type": "qq_onebot",
        "name": "研究生通知",
        "adapter_type": "qq_onebot",
        "config": {"base_url": "http://127.0.0.1:3000"},
        "interval_seconds": 1800,
    })


def conversation_ids(repo):
    with repo.db.connection() as conn:
        rows = conn.execute(
            "SELECT id, platform_conversation_id FROM conversations"
        ).fetchall()
    return {row["platform_conversation_id"]: row["id"] for row in rows}


def digest_jobs(repo):
    return [
        job
        for job in repo.list_jobs(limit=50)
        if job["type"] == "generate_digest"
    ]


def raw(conversation_id, name, message_id, text, published="2026-10-18T12:00:00Z"):
    return RawMessage(
        content_text=text,
        published_at=published,
        platform_message_id=message_id,
        conversation_id=conversation_id,
        conversation_name=name,
        conversation_kind="group",
        sender_name="同学",
    )


class QqDigestFlowTests(unittest.TestCase):
    def _summary_job(self, date_text, source_id="source-1"):
        return {
            "type": "summarize_conversation",
            "source_id": source_id,
            "payload_json": json.dumps(
                {"conversation_id": "conversation-1", "date": date_text}
            ),
        }

    def _summary_worker(self, config, repo, conversation_service):
        return Worker(
            repo,
            mock.Mock(),
            mock.Mock(),
            DigestService(config, repo),
            conversation_service,
        )

    def test_conversation_summary_waits_for_current_day_digest_time(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.scheduling.digest_time = "22:00"
            repo = build_repo(root)
            conversation_service = mock.Mock()
            worker = self._summary_worker(config, repo, conversation_service)
            before_due = datetime(2026, 10, 21, 13, 59, tzinfo=timezone.utc)
            with mock.patch(
                "msgdesk.services.scheduler.utc_now", return_value=before_due
            ):
                worker.dispatch(self._summary_job("2026-10-21"))

            conversation_service.summarize.assert_called_once_with(
                "source-1", "conversation-1", "2026-10-21"
            )
            self.assertEqual(digest_jobs(repo), [])

    def test_conversation_summary_enqueues_past_day_before_digest_time(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.scheduling.digest_time = "22:00"
            repo = build_repo(root)
            conversation_service = mock.Mock()
            worker = self._summary_worker(config, repo, conversation_service)
            before_due = datetime(2026, 10, 21, 13, 59, tzinfo=timezone.utc)
            with mock.patch(
                "msgdesk.services.scheduler.utc_now", return_value=before_due
            ):
                worker.dispatch(self._summary_job("2026-10-20"))

            jobs = digest_jobs(repo)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                json.loads(jobs[0]["payload_json"])["date"], "2026-10-20"
            )

    def test_conversation_summary_enqueues_at_digest_time_and_deduplicates(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.scheduling.digest_time = "22:00"
            repo = build_repo(root)
            conversation_service = mock.Mock()
            worker = self._summary_worker(config, repo, conversation_service)
            at_due = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
            with mock.patch(
                "msgdesk.services.scheduler.utc_now", return_value=at_due
            ):
                worker.dispatch(self._summary_job("2026-10-21"))
                worker.dispatch(self._summary_job("2026-10-21"))

            jobs = digest_jobs(repo)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(
                json.loads(jobs[0]["payload_json"])["date"], "2026-10-21"
            )

    def test_qq_sync_queues_one_summary_per_group(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = qq_source(repo)
            messages = [
                raw("g1", "群一", "1", "通知一"),
                raw("g1", "群一", "2", "通知二"),
                raw("g2", "群二", "3", "聊天"),
            ]
            service = SyncService(config, repo, adapter_factory=lambda _: FakeQQAdapter(messages))
            service.sync_source(source["id"])

            jobs = repo.list_jobs(limit=20)
            types = [job["type"] for job in jobs]
            self.assertNotIn("process_message", types)
            summaries = [job for job in jobs if job["type"] == "summarize_conversation"]
            self.assertEqual(len(summaries), 2)
            payloads = {json.loads(job["payload_json"])["conversation_id"] for job in summaries}
            ids = conversation_ids(repo)
            self.assertEqual(payloads, {ids["g1"], ids["g2"]})
            for job in summaries:
                self.assertEqual(json.loads(job["payload_json"])["date"], "2026-10-18")

    def test_digest_shows_group_summary_and_hides_raw_index(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = qq_source(repo)
            messages = [
                raw("g1", "群一", "1", "通知一"),
                raw("g1", "群一", "2", "通知二"),
            ]
            SyncService(config, repo, adapter_factory=lambda _: FakeQQAdapter(messages)).sync_source(
                source["id"]
            )
            ids = conversation_ids(repo)
            repo.upsert_conversation_digest(
                source_id=source["id"],
                conversation_id=ids["g1"],
                conversation_name="群一",
                date_text="2026-10-18",
                message_count=2,
                summary_md="今天在统计党员材料，截止 10 月 20 日。",
                model_name="test",
            )

            result = DigestService(config, repo).generate("2026-10-18")
            content = Path(result["file_path"]).read_text(encoding="utf-8")
            self.assertIn("## 群聊摘要", content)
            self.assertIn("### 群一（2 条）", content)
            self.assertIn("今天在统计党员材料", content)
            self.assertNotIn("原始消息索引", content)
            self.assertNotIn("通知一", content)

    def test_missing_digests_are_generated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = qq_source(repo)
            messages = [
                raw("g1", "群一", "1", "通知一"),
                raw("g1", "群一", "2", "通知二"),
            ]
            SyncService(config, repo, adapter_factory=lambda _: FakeQQAdapter(messages)).sync_source(
                source["id"]
            )
            service = DigestService(config, repo)
            self.assertEqual(repo.list_digests(10), [])
            self.assertEqual(service.generate_missing(), 1)
            digests = repo.list_digests(10)
            self.assertEqual([item["date"] for item in digests], ["2026-10-18"])

    def test_sync_backfill_skips_current_local_day(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = qq_source(repo)
            messages = [
                raw(
                    "g1", "群一", "1", "昨天的通知",
                    published="2026-10-20T12:00:00Z",
                ),
                raw(
                    "g1", "群一", "2", "今天的通知",
                    published="2026-10-21T01:00:00Z",
                ),
            ]
            sync_service = SyncService(
                config, repo, adapter_factory=lambda _: FakeQQAdapter(messages)
            )
            worker = Worker(repo, sync_service, mock.Mock(), DigestService(config, repo))
            fixed_now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
            with mock.patch(
                "msgdesk.services.digest.utc_now", return_value=fixed_now
            ):
                worker.dispatch({
                    "type": "sync_source",
                    "source_id": source["id"],
                    "payload_json": "{}",
                })
            self.assertIsNotNone(repo.get_digest("2026-10-20"))
            self.assertIsNone(repo.get_digest("2026-10-21"))

    def test_group_summary_marks_messages_processed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            source = qq_source(repo)
            messages = [
                raw("g1", "群一", "1", "通知一"),
                raw("g1", "群一", "2", "通知二"),
            ]
            SyncService(config, repo, adapter_factory=lambda _: FakeQQAdapter(messages)).sync_source(
                source["id"]
            )
            ids = conversation_ids(repo)
            with mock.patch(
                "msgdesk.services.conversation_digest.chat_text",
                return_value="今天在统计党员材料。",
            ):
                ConversationDigestService(config, repo).summarize(
                    source["id"], ids["g1"], "2026-10-18"
                )

            statuses = {
                message["platform_message_id"]: message["processing_status"]
                for message in repo.list_messages(limit=10)
            }
            self.assertEqual(statuses, {"1": "processed", "2": "processed"})


if __name__ == "__main__":
    unittest.main()
