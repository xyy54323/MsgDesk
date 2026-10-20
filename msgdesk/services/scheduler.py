"""Due-source and daily-digest scheduler."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from ..config import AppConfig
from ..repository import Repository
from ..timeutil import local_zone, parse_datetime, utc_now

log = logging.getLogger("msgdesk.scheduler")


class Scheduler:
    def __init__(self, config: AppConfig, repo: Repository):
        self.config = config
        self.repo = repo

    def tick(self) -> list[str]:
        created: list[str] = []
        now = utc_now()
        zone = local_zone(self.config.system.timezone)
        for source in self.repo.enabled_sources():
            if self.repo.has_active_job("sync_source", source_id=source["id"]):
                continue
            interval = int(source.get("interval_seconds") or 600)
            reference = parse_datetime(
                source.get("last_success_at") or source.get("last_error_at")
            )
            if reference is None or now - reference >= timedelta(seconds=interval):
                created.append(
                    self.repo.enqueue_job(
                        "sync_source", source_id=source["id"], priority=60
                    )
                )

        local_now = now.astimezone(zone)
        today = local_now.date().isoformat()
        digest_time = self._parse_time(self.config.scheduling.digest_time)
        if digest_time and local_now.time() >= digest_time:
            existing = self.repo.get_digest(today)
            generated_today = False
            if existing and existing.get("generated_at"):
                generated = parse_datetime(existing["generated_at"])
                generated_today = bool(generated and generated.astimezone(zone).date().isoformat() == today)
            if not generated_today and not self.repo.has_active_digest_job(today):
                created.append(
                    self.repo.enqueue_job(
                        "generate_digest", payload={"date": today}, priority=150
                    )
                )
        backup = self.config.data_dir / "backups" / f"messages-{today}.db"
        if not backup.exists() and not self.repo.has_active_job("maintenance"):
            created.append(self.repo.enqueue_job("maintenance", priority=200))
        return created

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            stop_event.wait(max(2.0, float(self.config.scheduling.poll_seconds)))

    @staticmethod
    def _parse_time(value: str) -> time | None:
        try:
            return datetime.strptime(value, "%H:%M").time()
        except (TypeError, ValueError):
            return None

