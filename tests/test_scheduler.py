import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from msgdesk.services.scheduler import Scheduler, digest_due_at
from tests.test_msgdesk import build_config, build_repo


TODAY = "2026-10-21"


def store_digest(repo, generated_at: str) -> None:
    with mock.patch("msgdesk.repository.iso_utc", return_value=generated_at):
        repo.upsert_digest(
            TODAY,
            file_path="generated.md",
            content_hash="0" * 64,
            message_count=0,
            urgent_count=0,
        )


def digest_jobs(repo) -> list[dict]:
    return [
        job for job in repo.list_jobs(limit=50)
        if job["type"] == "generate_digest"
    ]


class SchedulerDigestTests(unittest.TestCase):
    def test_digest_gate_allows_history_and_waits_for_current_day(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp))
            config.scheduling.digest_time = "22:00"
            before_due = datetime(2026, 10, 21, 13, 59, tzinfo=timezone.utc)
            at_due = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)

            self.assertIsNone(digest_due_at(config, TODAY, now=before_due))
            self.assertIsNotNone(
                digest_due_at(config, "2026-10-20", now=before_due)
            )
            self.assertIsNotNone(digest_due_at(config, TODAY, now=at_due))

    def test_digest_waits_for_local_digest_time(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp))
            config.scheduling.digest_time = "22:00"
            repo = build_repo(Path(tmp))
            scheduler = Scheduler(config, repo)
            before_due = datetime(2026, 10, 21, 13, 59, tzinfo=timezone.utc)
            with mock.patch(
                "msgdesk.services.scheduler.utc_now", return_value=before_due
            ):
                scheduler.tick()
            self.assertEqual(digest_jobs(repo), [])

    def test_early_digest_is_regenerated_after_local_digest_time(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp))
            config.scheduling.digest_time = "22:00"
            repo = build_repo(Path(tmp))
            store_digest(repo, "2026-10-21T13:00:00Z")
            scheduler = Scheduler(config, repo)
            at_due = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
            with mock.patch(
                "msgdesk.services.scheduler.utc_now", return_value=at_due
            ):
                scheduler.tick()
            jobs = digest_jobs(repo)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(json.loads(jobs[0]["payload_json"])["date"], TODAY)

    def test_digest_generated_after_schedule_is_not_repeated(self):
        with TemporaryDirectory() as tmp:
            config = build_config(Path(tmp))
            config.scheduling.digest_time = "22:00"
            repo = build_repo(Path(tmp))
            store_digest(repo, "2026-10-21T14:30:00Z")
            scheduler = Scheduler(config, repo)
            after_due = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)
            with mock.patch(
                "msgdesk.services.scheduler.utc_now", return_value=after_due
            ):
                scheduler.tick()
            self.assertEqual(digest_jobs(repo), [])


if __name__ == "__main__":
    unittest.main()
