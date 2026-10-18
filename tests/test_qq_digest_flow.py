import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.models import FetchResult, RawMessage, ValidationResult
from msgdesk.services.digest import DigestService
from msgdesk.services.sync import SyncService
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


if __name__ == "__main__":
    unittest.main()
