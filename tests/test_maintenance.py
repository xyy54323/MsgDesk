import os
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from msgdesk.services.maintenance import MaintenanceService
from msgdesk.timeutil import iso_utc, local_date_of, utc_now
from tests.test_msgdesk import build_config, build_repo


def today(config) -> date:
    return date.fromisoformat(
        local_date_of(utc_now().isoformat(), config.system.timezone)
    )


def seed_message(repo, text: str, published: str | None = None,
                 platform_id: str = "m1") -> str:
    source = repo.create_source({
        "type": "ai_web",
        "name": "网站",
        "adapter_type": "ai_web",
        "config": {"url": "https://example.com/"},
        "default_category": "通知",
        "interval_seconds": 600,
    })
    with repo.db.connection() as conn:
        conversation = repo.upsert_conversation(
            conn, source["id"], "c1", "网站", "web"
        )
        return repo.insert_message(conn, {
            "source_id": source["id"],
            "conversation_id": conversation,
            "platform_message_id": platform_id,
            "published_at": published or iso_utc(),
            "collected_at": iso_utc(),
            "content_text": text,
            "content_hash": f"hash-{platform_id}",
        })


class MaintenanceTests(unittest.TestCase):
    def test_backup_creates_daily_snapshot_and_keeps_seven(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            service = MaintenanceService(config, repo)
            result = service.backup_database()
            self.assertTrue(Path(result["backup_path"]).exists())
            backups = config.data_dir / "backups"
            for index in range(9):
                (backups / f"messages-2020-01-{index + 1:02d}.db").write_bytes(b"x")
            service.backup_database()
            self.assertLessEqual(len(list(backups.glob("messages-*.db"))), 7)

    def test_orphan_cleanup_keeps_referenced_and_recent_files(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            repo = build_repo(root)
            folder = config.data_dir / "media" / "2026-01-01"
            folder.mkdir(parents=True)
            old = folder / "old.png"
            keep = folder / "keep.png"
            fresh = folder / "fresh.png"
            for path in (old, keep, fresh):
                path.write_bytes(b"img")
            stale = utc_now().timestamp() - 3 * 86400
            os.utime(old, (stale, stale))
            os.utime(keep, (stale, stale))
            seed_message(repo, "![图片](/media/2026-01-01/keep.png)")

            stats = MaintenanceService(config, repo).clean_orphans()

            self.assertFalse(old.exists())
            self.assertTrue(keep.exists())
            self.assertTrue(fresh.exists())
            self.assertEqual(stats["orphans_removed"], 1)

    def test_retention_removes_old_media_and_strips_references(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.system.retention_days = 30
            repo = build_repo(root)
            now = today(config)
            old_day = (now - timedelta(days=45)).isoformat()
            new_day = (now - timedelta(days=5)).isoformat()
            old_file = config.data_dir / "media" / old_day / "a.png"
            new_file = config.data_dir / "media" / new_day / "b.png"
            old_file.parent.mkdir(parents=True)
            new_file.parent.mkdir(parents=True)
            old_file.write_bytes(b"old")
            new_file.write_bytes(b"new")
            message_id = seed_message(
                repo,
                f"正文\n\n![图片](/media/{old_day}/a.png)\n\n![图片](/media/{new_day}/b.png)",
            )
            old_published = (
                datetime.now(timezone.utc) - timedelta(days=60)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            old_message_id = seed_message(
                repo, "旧消息正文", published=old_published, platform_id="m-old"
            )
            raw = (
                config.data_dir
                / "raw"
                / old_day[:4]
                / old_day[5:7]
                / old_day[8:10]
                / "snapshot.json"
            )
            raw.parent.mkdir(parents=True)
            raw.write_text("{}", encoding="utf-8")

            stats = MaintenanceService(config, repo).apply_retention()

            self.assertFalse(old_file.exists())
            self.assertTrue(new_file.exists())
            self.assertFalse(raw.exists())
            self.assertGreaterEqual(stats["retained_removed"], 2)
            self.assertEqual(stats["retained_messages"], 1)
            with repo.db.connection() as conn:
                text = conn.execute(
                    "SELECT content_text FROM messages WHERE id = ?", (message_id,)
                ).fetchone()["content_text"]
                old = conn.execute(
                    "SELECT 1 FROM messages WHERE id = ?", (old_message_id,)
                ).fetchone()
            self.assertIsNone(old)
            self.assertNotIn(old_day, text)
            self.assertIn(new_day, text)

    def test_retention_can_be_disabled(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.system.retention_days = 0
            repo = build_repo(root)
            old_file = config.data_dir / "media" / "2020-01-01" / "a.png"
            old_file.parent.mkdir(parents=True)
            old_file.write_bytes(b"old")
            stats = MaintenanceService(config, repo).apply_retention()
            self.assertEqual(stats["retained_removed"], 0)
            self.assertTrue(old_file.exists())


if __name__ == "__main__":
    unittest.main()
