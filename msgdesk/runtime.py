"""Composition root for services, workers and the scheduler."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .config import AppConfig, merge_config_file
from .db import Database
from .repository import Repository, loads
from .services.digest import DigestService
from .services.model import create_model_provider
from .services.processing import ProcessingService
from .services.scheduler import Scheduler
from .services.session_keeper import SessionKeeper
from .services.conversation_digest import ConversationDigestService
from .services.sync import SyncService
from .services.worker import Worker

log = logging.getLogger("msgdesk.runtime")


class Runtime:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database(config.db_path)
        self.db.migrate()
        self.repo = Repository(self.db)
        self.repo.recover_interrupted_jobs()
        self._seed_legacy_range()
        self.model = create_model_provider(config.model, config.system.timezone)
        self.sync_service = SyncService(config, self.repo)
        self.processing_service = ProcessingService(config, self.repo, self.model)
        self.digest_service = DigestService(config, self.repo)
        self.conversation_digest_service = ConversationDigestService(config, self.repo)
        self.worker = Worker(
            self.repo,
            self.sync_service,
            self.processing_service,
            self.digest_service,
            self.conversation_digest_service,
        )
        self.scheduler = Scheduler(config, self.repo)
        self.session_keeper = SessionKeeper(config, self.repo)
        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []

    def start_background(self) -> None:
        count = max(1, min(int(self.config.scheduling.max_parallel_jobs), 8))
        for index in range(count):
            thread = threading.Thread(
                target=self.worker.run_forever,
                args=(self.stop_event,),
                name=f"msgdesk-worker-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        scheduler_thread = threading.Thread(
            target=self.scheduler.run_forever,
            args=(self.stop_event,),
            name="msgdesk-scheduler",
            daemon=True,
        )
        scheduler_thread.start()
        self._threads.append(scheduler_thread)
        keeper_thread = threading.Thread(
            target=self.session_keeper.run_forever,
            args=(self.stop_event,),
            name="msgdesk-session-keeper",
            daemon=True,
        )
        keeper_thread.start()
        self._threads.append(keeper_thread)
        backfill = threading.Thread(
            target=self._generate_missing_digests,
            name="msgdesk-digest-backfill",
            daemon=True,
        )
        backfill.start()
        self._threads.append(backfill)
        log.info("runtime started with %s workers", count)

    def _seed_legacy_range(self) -> None:
        """Use the earliest old QQ history date as the first global start."""
        if self.config.date_range.start:
            return
        starts = []
        for source in self.repo.list_sources():
            source_config = loads(source.get("config_json"), {}) or {}
            value = str(source_config.get("history_from") or "").strip()
            if value:
                starts.append(value)
        if not starts:
            return
        self.config.date_range.start = min(starts)
        if self.config.config_path:
            merge_config_file(
                self.config.config_path,
                {"range": {"start": self.config.date_range.start, "end": self.config.date_range.end}},
            )

    def _generate_missing_digests(self) -> None:
        try:
            count = self.digest_service.generate_missing()
            if count:
                log.info("generated %s missing digests", count)
        except Exception:  # noqa: BLE001 - never block startup
            log.exception("digest backfill failed")

    def stop(self) -> None:
        self.stop_event.set()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()

    def apply_model_config(self) -> None:
        """Rebuild the model provider after settings are saved in memory."""
        self.model = create_model_provider(self.config.model, self.config.system.timezone)
        self.processing_service.model = self.model

    def drain(self, max_jobs: int = 200) -> int:
        processed = 0
        while processed < max_jobs and self.worker.run_once():
            processed += 1
        return processed
