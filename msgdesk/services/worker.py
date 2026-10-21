"""Persistent job worker."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from ..errors import AppError
from ..repository import Repository, loads
from .digest import DigestService
from .conversation_digest import ConversationDigestService
from .processing import ProcessingService
from .scheduler import digest_due_at
from .sync import SyncService
from .maintenance import MaintenanceService

log = logging.getLogger("msgdesk.worker")


class Worker:
    def __init__(
        self,
        repo: Repository,
        sync_service: SyncService,
        processing_service: ProcessingService,
        digest_service: DigestService,
        conversation_digest_service: ConversationDigestService | None = None,
        maintenance_service: MaintenanceService | None = None,
    ):
        self.repo = repo
        self.sync_service = sync_service
        self.processing_service = processing_service
        self.digest_service = digest_service
        self.conversation_digest_service = conversation_digest_service
        self.maintenance_service = maintenance_service

    def run_once(self) -> bool:
        job = self.repo.claim_next_job()
        if job is None:
            return False
        try:
            self.dispatch(job)
        except AppError as exc:
            log.warning("job %s failed: %s", job["id"], exc)
            self.repo.finish_job(
                job["id"],
                error_code=exc.code,
                error_message=exc.message[:500],
                retryable=exc.retryable,
            )
        except Exception as exc:
            log.exception("job %s crashed", job["id"])
            self.repo.finish_job(
                job["id"],
                error_code="INTERNAL_ERROR",
                error_message=f"{type(exc).__name__}: {str(exc)[:300]}",
                retryable=False,
            )
        else:
            self.repo.finish_job(job["id"])
        return True

    def dispatch(self, job: dict[str, Any]) -> None:
        payload = loads(job.get("payload_json"), {}) or {}
        job_type = job["type"]
        if job_type == "sync_source":
            source_id = job.get("source_id") or payload.get("source_id")
            if not source_id:
                raise AppError("VALIDATION_ERROR", "sync_source 任务缺少 source_id")
            self.sync_service.sync_source(source_id, max_items=int(payload.get("max_items", 50)))
            # Any day that received messages but never got a digest is filled in
            # here, so old days are not left blank.
            self.digest_service.generate_missing()
        elif job_type == "process_message":
            message_id = job.get("message_id") or payload.get("message_id")
            if not message_id:
                raise AppError("VALIDATION_ERROR", "process_message 任务缺少 message_id")
            self.processing_service.process(
                message_id, force=bool(payload.get("force")), raise_on_model_error=True
            )
        elif job_type == "summarize_conversation":
            source_id = job.get("source_id") or payload.get("source_id")
            conversation_id = str(payload.get("conversation_id") or "")
            date_text = str(payload.get("date") or "")
            if not source_id or not conversation_id or not date_text:
                raise AppError("VALIDATION_ERROR", "summarize_conversation 缺少参数")
            if self.conversation_digest_service is not None:
                self.conversation_digest_service.summarize(
                    source_id, conversation_id, date_text
                )
                if (
                    digest_due_at(self.digest_service.config, date_text) is not None
                    and not self.repo.open_digest_job_exists(date_text)
                ):
                    self.repo.enqueue_job(
                        "generate_digest",
                        payload={"date": date_text, "notify": False},
                        priority=150,
                    )
        elif job_type == "generate_digest":
            self.digest_service.generate(payload.get("date"))
        elif job_type == "maintenance":
            if self.maintenance_service is None:
                raise AppError("VALIDATION_ERROR", "维护服务未初始化")
            self.maintenance_service.run()
        else:
            raise AppError("VALIDATION_ERROR", f"未知任务类型: {job_type}")

    def run_forever(self, stop_event: threading.Event, poll_seconds: float = 1.0) -> None:
        while not stop_event.is_set():
            try:
                worked = self.run_once()
            except Exception:
                log.exception("worker loop error")
                worked = False
            if not worked:
                stop_event.wait(poll_seconds)


def enqueue_run_all(repo: Repository) -> list[str]:
    """Queue a sync for every enabled source; used by the overview button."""
    job_ids = []
    for source in repo.enabled_sources():
        if repo.has_active_job("sync_source", source_id=source["id"]):
            continue
        job_ids.append(
            repo.enqueue_job("sync_source", source_id=source["id"], priority=40)
        )
    return job_ids

